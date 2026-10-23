"""Transport only. Never deletes observation data or makes retention decisions."""
import configparser,hashlib,json,os,pathlib,selectors,signal,subprocess,time
class TransferError(RuntimeError):
    def __init__(self,code):super().__init__(code);self.code=code
class Rclone:
    def __init__(self,c):self.c=c
    def base(self):
        c=self.c['sync'];args=[c['rclone_executable']]
        if c['rclone_config_path']:args+=['--config',c['rclone_config_path']]
        return args
    def run(self,args,timeout,expected_bytes=None,capture=False):
        # No stderr containing remote credentials is persisted. Drain stdout in
        # bounded chunks; verification downloads are hashed, never staged on disk.
        process=subprocess.Popen(self.base()+args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
        sha=hashlib.sha256();total=0;captured=bytearray();selector=selectors.DefaultSelector();selector.register(process.stdout,selectors.EVENT_READ)
        deadline=time.monotonic()+timeout
        try:
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TransferError('timeout')
                for key,_ in selector.select(min(remaining,1)):
                    block=os.read(key.fd,65536)
                    if not block:selector.unregister(key.fileobj);continue
                    total+=len(block)
                    limit=expected_bytes if expected_bytes is not None else self.c['status']['max_bytes']
                    if total>limit:raise TransferError('integrity_size' if expected_bytes is not None else 'output_limit')
                    sha.update(block)
                    if capture:captured.extend(block)
            rc=process.wait(timeout=max(.001,deadline-time.monotonic()))
            if rc:raise TransferError('rclone_exit_'+str(rc))
            if expected_bytes is not None and total!=expected_bytes:raise TransferError('integrity_size')
            return bytes(captured) if capture else (sha.hexdigest(),total)
        except BaseException:
            if process.poll() is None:os.killpg(process.pid,signal.SIGKILL)
            process.wait();raise
        finally:selector.close();process.stdout.close()
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
