"""Transport only. Never deletes observation data or makes retention decisions."""
import configparser,hashlib,json,os,pathlib,re,selectors,signal,subprocess,time
# Only fixed diagnostic phrases leave the subprocess boundary. Raw stderr can
# contain credentials or arbitrary server-controlled text and is never logged.
def diagnostic_hint(stderr):
    text=stderr.decode('utf-8','replace').lower()
    cases=(
        (('storagequotaexceeded','storage quota exceeded'), 'Google Drive storage quota exceeded'),
        (('ratelimitexceeded','rate_limit_exceeded','rate limit exceeded','too many requests'), 'Google Drive API rate limit exceeded'),
        (('quota exceeded for quota metric','dailylimitexceeded'), 'Google API request quota exceeded'),
        (('invalid_grant','token has been expired or revoked','expired or revoked'), 'OAuth grant invalid, expired or revoked; reconnect rclone'),
        (('invalid_client','unauthorized_client'), 'OAuth client credentials rejected'),
        (('access_denied','insufficientpermissions','permission denied','forbidden'), 'Access denied or insufficient permission'),
        (('filenotfound','directory not found','404 not found'), 'Remote file or directory not found'),
        (('no such host','network is unreachable','connection refused','connection reset','i/o timeout','tls handshake timeout'), 'Network, DNS or connection failure'),
        (('failed to create file system','didn\'t find section in config file'), 'Rclone remote configuration failure'),
    )
    for needles,hint in cases:
        if any(n in text for n in needles):return hint
    return ''

class TransferError(RuntimeError):
    def __init__(self,code,detail=''):
        self.code=code;self.detail=detail
        super().__init__(code+(': '+detail if detail else ''))

def safe_error(error):
    if isinstance(error,TransferError):return str(error)
    if isinstance(error,subprocess.TimeoutExpired):return 'timeout'
    if isinstance(error,OSError):return type(error).__name__+': errno='+str(error.errno)
    # Keep controlled error codes, never an arbitrary exception message.
    message=str(error)
    code=re.match(r'^(rclone_exit_-?\d+|quicklook_exit_-?\d+)(?=$|:|\s)',message)
    if code:return code.group(1)
    known=('integrity_size','integrity_sha256','timeout','output_limit',
           'local artifact size changed','local artifact digest changed',
           'local artifact changed during upload','destination identity changed during upload',
           'startup probe failed; SDR must remain off')
    return message if message in known else type(error).__name__

class Rclone:
    def __init__(self,c):self.c=c
    def base(self):
        c=self.c['sync'];args=[c['rclone_executable']]
        if c['rclone_config_path']:args+=['--config',c['rclone_config_path']]
        return args
    def run(self,args,timeout,expected_bytes=None,capture=False):
        # Drain BOTH pipes: a verbose failure must not block a streaming verify.
        # Keep at most 16 KiB of stderr in memory solely for classification.
        process=subprocess.Popen(self.base()+args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        sha=hashlib.sha256();total=0;captured=bytearray();stderr_tail=bytearray();hint=''
        selector=selectors.DefaultSelector()
        selector.register(process.stdout,selectors.EVENT_READ,'stdout')
        selector.register(process.stderr,selectors.EVENT_READ,'stderr')
        deadline=time.monotonic()+timeout
        try:
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TransferError('timeout',hint)
                for key,_ in selector.select(min(remaining,1)):
                    block=os.read(key.fd,65536)
                    if not block:selector.unregister(key.fileobj);continue
                    if key.data=='stderr':
                        stderr_tail.extend(block);del stderr_tail[:-16384]
                        hint=diagnostic_hint(stderr_tail) or hint
                        continue
                    total+=len(block)
                    limit=expected_bytes if expected_bytes is not None else self.c['status']['max_bytes']
                    if total>limit:raise TransferError('integrity_size' if expected_bytes is not None else 'output_limit')
                    sha.update(block)
                    if capture:captured.extend(block)
            try:rc=process.wait(timeout=max(.001,deadline-time.monotonic()))
            except subprocess.TimeoutExpired:raise TransferError('timeout',hint) from None
            if rc:raise TransferError('rclone_exit_'+str(rc),hint)
            if expected_bytes is not None and total!=expected_bytes:raise TransferError('integrity_size')
            return bytes(captured) if capture else (sha.hexdigest(),total)
        except BaseException:
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
            process.wait();raise
        finally:
            selector.close();process.stdout.close();process.stderr.close()
    def identity(self):
        c=self.c['sync'];path=c['rclone_config_path']
        if not path:
            output=self.run(['config','file'],self.c['ledger']['busy_timeout_sec'],capture=True).decode()
            candidates=[pathlib.Path(line.strip()) for line in output.splitlines() if pathlib.Path(line.strip()).is_absolute() and pathlib.Path(line.strip()).is_file()]
            if len(candidates)!=1:raise TransferError('unresolved_rclone_config_identity')
            path=str(candidates[0])
        if pathlib.Path(path).stat().st_size>self.c["budget"]["metadata_headroom_bytes"]:raise TransferError("rclone_config_identity_size")
        raw=pathlib.Path(path).read_bytes()
        def stable_token(value):
            try:
                token=json.loads(value)
                if isinstance(token,dict):
                    fields=('access_token','expiry','token_type') if token.get('refresh_token') else ('expiry','token_type')
                    for field in fields:token.pop(field,None)
                    return json.dumps(token,sort_keys=True)
            except (ValueError,TypeError):pass
            return value
        try:
            parser=configparser.ConfigParser(interpolation=None);parser.read_string(raw.decode())
            contents={section:{key:stable_token(value) if key=='token' else value for key,value in parser[section].items()} for section in parser.sections()}
            content_bytes=json.dumps(contents,sort_keys=True).encode()
        except Exception:content_bytes=raw
        env={k:stable_token(v) if k.endswith('_TOKEN') else v for k,v in os.environ.items() if k.startswith('RCLONE_CONFIG')}
        stamp=hashlib.sha256(content_bytes+b'\0'+json.dumps(env,sort_keys=True).encode()).hexdigest()
        return c['remote_name']+':'+c['remote_base_dir']+'#'+stamp
    def copy(self,path,target,timeout):
        c=self.c['sync'];args=['copyto',str(path),target,'--transfers',str(c['transfers']),'--checkers',str(c['checkers']),'--retries','1','--low-level-retries','1','--bwlimit',c['bandwidth_limit']]
        self.run(args,timeout)
    def verify(self,target,size,sha,timeout):
        got,_=self.run(['cat',target],timeout,size)
        if got!=sha:raise TransferError('integrity_sha256')
    def remove_probe(self,target,timeout):self.run(['deletefile',target],timeout)
