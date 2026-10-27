#!/usr/bin/env python3
"""Launch one explicitly selected local executable under LLDB; capture only
PBKDF arguments matching salts from selected local databases. Save verified raw
keys privately; never print secrets. Does not attach to arbitrary processes,
change signatures, obtain privileges, or persist administrator credentials.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import time

# Adapted from the MIT-licensed macOS toolkit by acmerfight and contributors.
# Pinned source: https://gist.githubusercontent.com/acmerfight/0a01249ef72970d07a2603dbb629e80f/raw/021410a10b1a5d4139b18608eb05f0d7de2b5e36/capture_keys.py
# Full license: scripts/macos/LICENSE.


def emit(kind, **kw):
    print(json.dumps(dict(type=kind, **kw)), flush=True)


def verified(key, page):
    if len(key) != 32 or len(page) != 4096:
        return False
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(x ^ 0x3a for x in page[:16]), 2, 32)
    actual = hmac.new(mac_key, page[16:4032]+b'\x01\x00\x00\x00', hashlib.sha512).digest()
    return hmac.compare_digest(actual, page[4032:4096])


def validate_executable(path):
    """Only a manually prepared temporary copy may be launched."""
    candidate = Path(path).expanduser().resolve()
    installed = (Path('/Applications'), (Path.home() / 'Applications').resolve())
    if any(candidate == base or base in candidate.parents for base in installed):
        raise RuntimeError('installed applications cannot be capture targets; use a temporary copy')
    if not candidate.is_file():
        raise RuntimeError('capture executable does not exist')
    return candidate


def private_json(path, data):
    """Publish one complete capture privately, without overwriting any file."""
    import tempfile
    p = Path(path)
    if p.parent.is_symlink():
        raise RuntimeError('output directory cannot be a symbolic link')
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if p.parent.stat().st_mode & 0o077:
        raise RuntimeError('output directory must have private permissions (0700)')
    fd, temporary = tempfile.mkstemp(prefix='.capture-', dir=p.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, p)
    finally:
        os.unlink(temporary)


def unreadable_directory(_error):
    raise RuntimeError('cannot completely read database directory; check access permissions') from None


def database_rows(root):
    """Collect every encrypted page, aborting if any directory is unreadable."""
    rows = []
    for directory, directories, files in os.walk(
            root, followlinks=False, onerror=unreadable_directory):
        if any((Path(directory) / name).is_symlink() for name in directories + files):
            raise RuntimeError('database directory cannot contain symbolic links')
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix != '.db':
                continue
            if not stat.S_ISREG(path.stat().st_mode):
                raise RuntimeError('database must be a regular file')
            with path.open('rb') as source:
                page = source.read(4096)
            if page.startswith(b'SQLite format 3\0'):
                continue
            if len(page) != 4096 or path.stat().st_size % 4096:
                raise RuntimeError('invalid encrypted database size')
            rows.append((str(path.relative_to(root)), page))
    if not rows:
        raise RuntimeError('no encrypted database pages to validate')
    return rows


def close_owned_process(process, lldb):
    """Report closure only after LLDB confirms that our child exited."""
    if process is None or not process.IsValid():
        return
    if process.GetState() in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateInvalid):
        return
    error = process.Kill()
    if not error.Success() or process.GetState() != lldb.eStateExited:
        emit('owned_process_cleanup_failed', pid=process.GetProcessID())
        raise RuntimeError('could not close owned capture process; close the temporary copy manually')
    emit('owned_process_closed', pid=process.GetProcessID())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exe', required=True)
    ap.add_argument('--db-root', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--timeout', type=int, default=150)
    args = ap.parse_args()
    if os.path.lexists(args.out):
        raise RuntimeError("output already exists; select a new private filename")
    exe = validate_executable(args.exe)
    if Path(args.db_root).is_symlink():
        raise RuntimeError('database root cannot be a symbolic link')
    root = Path(args.db_root).resolve(strict=True)
    rows = database_rows(root)
    salts = {page[:16] for _,page in rows}
    macsalts = {bytes(x^0x3a for x in s): s for s in salts}
    found = {}
    counts = {'pbkdf_calls':0,'matched_salt_calls':0}
    os.umask(0o077)
    import lldb
    debugger = lldb.SBDebugger.Create()
    debugger.SetAsync(True)
    debugger.SkipLLDBInitFiles(True)
    listener = debugger.GetListener()
    target = debugger.CreateTarget(str(exe))
    if not target.IsValid(): raise RuntimeError('invalid launch target')
    bp = target.BreakpointCreateByName('CCKeyDerivationPBKDF')
    if not bp.IsValid(): raise RuntimeError('breakpoint creation failed')
    launch = lldb.SBLaunchInfo([])
    launch.AddSuppressFileAction(0, True, False)
    launch.AddSuppressFileAction(1, False, True)
    launch.AddSuppressFileAction(2, False, True)
    launch.SetWorkingDirectory(str(exe.parent))
    err = lldb.SBError()
    process = None
    try:
        process = target.Launch(launch, err)
        if err.Fail() or not process.IsValid():
            emit('launch_failed', error=err.GetCString())
            return 2
        emit('launched', pid=process.GetProcessID(), database_count=len(rows), breakpoint_locations=bp.GetNumLocations())
        deadline = time.monotonic()+min(max(args.timeout,10),300)
        last_update = time.monotonic()
        def mem(addr,size):
            e=lldb.SBError(); b=process.ReadMemory(addr,size,e)
            return bytes(b) if e.Success() and len(b)==size else b''
        while time.monotonic()<deadline:
            event=lldb.SBEvent()
            if not listener.WaitForEvent(1,event):
                if time.monotonic()-last_update>15:
                    emit("waiting",verified_databases=len(found),**counts)
                    last_update=time.monotonic()
                continue
            if not lldb.SBProcess.EventIsProcessEvent(event): continue
            state=lldb.SBProcess.GetStateFromEvent(event)
            if state in (lldb.eStateExited,lldb.eStateCrashed,lldb.eStateDetached):
                emit('process_ended',state=state,exit_status=process.GetExitStatus())
                break
            if state==lldb.eStateStopped:
                for thread in process:
                    if thread.GetStopReason()!=lldb.eStopReasonBreakpoint: continue
                    if thread.GetStopReasonDataCount()<2 or thread.GetStopReasonDataAtIndex(0)!=bp.GetID(): continue
                    counts['pbkdf_calls']+=1
                    frame=thread.GetFrameAtIndex(0)
                    reg=lambda name: frame.FindRegister(name).GetValueAsUnsigned()
                    if not target.GetTriple().startswith('arm64'):
                        raise RuntimeError('only arm64 capture implemented')
                    pp,pl,sp,sl,prf,rounds=[reg('x'+str(i)) for i in range(1,7)]
                    if not (sl==16 and 0<pl<=256 and prf==5 and rounds in (2,256000)): continue
                    salt_arg=mem(sp,16)
                    salt=salt_arg if rounds==256000 else macsalts.get(salt_arg)
                    if salt not in salts: continue
                    counts['matched_salt_calls']+=1
                    secret=mem(pp,pl)
                    if len(secret)!=pl: continue
                    key=hashlib.pbkdf2_hmac('sha512',secret,salt,256000,32) if rounds==256000 else secret
                    if len(key)!=32: continue
                    changed=False
                    for rel,page in rows:
                        if rel not in found and page[:16]==salt and verified(key,page):
                            found[rel]={'enc_key':key.hex()}
                            changed=True
                    if changed:
                        emit('verified_progress',verified_databases=len(found),total_databases=len(rows),**counts)
                # Resume only this launched process; no external attach or process-name cleanup.
                e=process.Continue()
                if e.Fail(): raise RuntimeError('could not resume launched process')
            if len(found)==len(rows): break
            if time.monotonic()-last_update>15:
                emit('waiting',verified_databases=len(found),**counts)
                last_update=time.monotonic()
        if len(found) == len(rows):
            private_json(args.out, {'keys':found})
        emit('capture_finished',verified_databases=len(found),total_databases=len(rows),**counts)
        return 0 if len(found) == len(rows) else 3
    finally:
        try:
            close_owned_process(process, lldb)
        finally:
            lldb.SBDebugger.Destroy(debugger)


if __name__=='__main__':
    try: result=main()
    except Exception as e:
        emit('capture_error',error=str(e)); result=2
    raise SystemExit(result)
