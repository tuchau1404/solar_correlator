import contextlib,fcntl,os,pathlib
@contextlib.contextmanager
def lock(path,exclusive=True):
    p=pathlib.Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(p,os.O_CREAT|os.O_RDWR|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(fd,(fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)|fcntl.LOCK_NB)
        except BlockingIOError:yield None;return
        yield fd
    finally:os.close(fd)
def artifact_lock(c,aid):
    if not aid or any(x not in '0123456789abcdef-_' for x in aid):raise ValueError('invalid artifact id')
    return pathlib.Path(c['ledger']['path']).parent/'artifact_locks'/f'{aid}.lock'
def owned_path(c,path):
    p=pathlib.Path(path);root=pathlib.Path(c['storage']['data_dir'])
    if not p.is_absolute() or not p.is_relative_to(root) or p==root:raise RuntimeError('artifact outside data_dir')
    if any(x.is_symlink() for x in [p,*p.parents]):raise RuntimeError('symlink artifact rejected')
    if p.resolve()!=p:raise RuntimeError('noncanonical artifact path')
    return p
