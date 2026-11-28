"""LinuxワーカーのFS境界。Landlockなし・未知ABIでは拒否し、弱い代替へ落とさない。"""
import ctypes
import os
import platform
import signal
import sys


def restrict_filesystem(root_fd, *, writable=False, expected_parent, readonly_fds=()):
    if sys.platform!='linux' or platform.machine() not in ('x86_64','amd64') or os.geteuid()==0:
        raise RuntimeError('Supported Linux non-root worker required')
    libc=ctypes.CDLL(None,use_errno=True)
    # 親の停止後に収集を継続しない。設定前に親が消えた競合も検出する。
    if libc.prctl(1,signal.SIGKILL,0,0,0)!=0 or os.getppid()!=expected_parent:raise RuntimeError('Worker parent unavailable')
    if libc.prctl(38,1,0,0,0)!=0 or libc.prctl(4,0,0,0,0)!=0:raise RuntimeError('Worker privilege restriction failed')
    import resource
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    resource.setrlimit(resource.RLIMIT_NPROC,(0,0))
    resource.setrlimit(resource.RLIMIT_CPU,(8,8))
    resource.setrlimit(resource.RLIMIT_FSIZE,(2*1024*1024,2*1024*1024))
    abi=libc.syscall(444,0,0,1)
    if abi<3:raise RuntimeError('Landlock ABI 3 or later required')
    class Ruleset(ctypes.Structure):_fields_=[('handled_access_fs',ctypes.c_uint64)]
    class PathRule(ctypes.Structure):
        _pack_=1
        _fields_=[('allowed_access',ctypes.c_uint64),('parent_fd',ctypes.c_int32)]
    # EXECUTE・WRITE・REMOVE・MAKE・REFER・TRUNCATEまで処理対象にする。
    handled=(1<<15)-1;read=(1<<2)|(1<<3)
    allowed=read|sum(1<<bit for bit in (1,4,5,7,8,13,14)) if writable else read
    rules=Ruleset(handled);rules_fd=libc.syscall(444,ctypes.byref(rules),ctypes.sizeof(rules),0)
    if rules_fd<0:raise RuntimeError('Landlock ruleset unavailable')
    try:
        rule=PathRule(allowed,root_fd)
        if libc.syscall(445,rules_fd,1,ctypes.byref(rule),0)!=0:raise RuntimeError('Landlock restriction failed')
        for fd in readonly_fds:
            metadata=PathRule(read,fd)
            if libc.syscall(445,rules_fd,1,ctypes.byref(metadata),0)!=0:raise RuntimeError('Landlock metadata rule failed')
        if libc.syscall(446,rules_fd,0)!=0:
            raise RuntimeError('Landlock restriction failed')
    finally:os.close(rules_fd)
