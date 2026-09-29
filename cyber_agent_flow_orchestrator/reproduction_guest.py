import hashlib,json,os,stat,sys,tempfile,zipfile
from pathlib import Path

def main(data):
    source=data['source']
    repo=Path(data['repo']).resolve()
    allowed=[repo/'outputs',Path('/tmp/vulns/flag_generators_runs'),Path('/tmp/vulns/flag_node_generators_runs')]
    candidates=[Path(source)]
    for prefix,kind in [('/tmp/vulns/flag_generators_runs/','flag_generators_runs'),('/tmp/vulns/flag_node_generators_runs/','flag_node_generators_runs')]:
        if source.startswith(prefix):
            suffix=source[len(prefix):]
            candidates += [repo/'outputs'/kind/suffix,repo/'outputs/vulns'/kind/suffix]
    root=None
    for candidate in candidates:
        if candidate.is_symlink():continue
        resolved=candidate.resolve()
        if any(resolved.is_relative_to(base) for base in allowed) and resolved.is_dir():
            root=resolved;break
    if root is None:return {'available':False}
    entries=[];total=0
    for folder,dirs,names in os.walk(root,followlinks=False):
        dirs[:]=[name for name in dirs if not (Path(folder)/name).is_symlink()]
        for name in names:
            path=Path(folder)/name
            if path.is_symlink() or not path.is_file():continue
            info=path.stat()
            if not stat.S_ISREG(info.st_mode):continue
            total+=info.st_size
            if total>data['limit'] or len(entries)>=9990:raise ValueError('Referenced artifacts exceed capture limit')
            entries.append(path)
    fd,name=tempfile.mkstemp(prefix='caf-reproduction-',suffix='.zip');os.close(fd)
    try:
        records=[]
        with zipfile.ZipFile(name,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(entries):
                relative=path.relative_to(root).as_posix()
                digest=hashlib.sha256();size=0
                with path.open('rb') as source_file,archive.open('files/'+relative,'w') as target:
                    for block in iter(lambda:source_file.read(1024*1024),b''):
                        size+=len(block)
                        if size>data['limit']:raise ValueError('Artifact grew during capture')
                        digest.update(block);target.write(block)
                records.append({'path':relative,'sha256':digest.hexdigest(),'size':size,'mode':stat.S_IMODE(path.stat().st_mode)})
            archive.writestr('files.json',json.dumps(records))
        if Path(name).stat().st_size>data['limit']:raise ValueError('Capture exceeds transfer limit')
        return {'available':True,'path':name}
    except BaseException:
        Path(name).unlink(missing_ok=True);raise

if __name__ == '__main__':
    try:
        print(json.dumps(main(json.loads(sys.argv[1]))))
    except Exception as exc:
        print(json.dumps({'error':str(exc)}));raise SystemExit(1)
