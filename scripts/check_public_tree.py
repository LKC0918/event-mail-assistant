"""Conservative release audit. Reports categories and paths, never matched values."""
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT_FILES={'README.md','LICENSE','pyproject.toml','.gitignore','.env.example'}
FOLDERS={'event_assistant':{'.py'},'tests':{'.py'},'scripts':{'.py'},
         'docs':{'.md'},'examples':{'.example.json','.json'},
         'deploy':{'.service'},'.github':{'.yml','.yaml'}}
LOCAL_ONLY={'.git','.venv','venv','__pycache__','state','previews','private','artifacts','dist','build'}


def discover(root):
    root=Path(root)
    if (root/'.git').exists():
        result=subprocess.run(['git','ls-files','--cached','--others','--exclude-standard','-z'],
                              cwd=root,check=True,capture_output=True)
        return sorted({Path(name.decode()) for name in result.stdout.split(b'\0') if name})
    return sorted(path.relative_to(root) for path in root.rglob('*') if path.is_file()
                  and not set(path.relative_to(root).parts)&LOCAL_ONLY
                  and not path.name.endswith('.pyc') and path.name!='config.json'
                  and (not path.name.startswith('.env') or path.name=='.env.example'))


def categories(text):
    found=set()
    if re.search(r'/(?:Users|home)/[^/\s]+/',text):found.add('personal_path')
    if re.search(r'(?i)\b[a-z0-9.-]+\.edu(?:\.[a-z]{2})?\b',text):found.add('institution_domain')
    for address in re.findall(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}',text):
        domain=address.split('@')[-1].lower()
        if domain!='example.com' and not domain.endswith('.example.com'):
            found.add('nonexample_email')
    for value in re.findall(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])',text):
        try:
            if ipaddress.ip_address(value).is_global:found.add('public_ip')
        except ValueError:pass
    if re.search(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[A-Z0-9]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)',text):
        found.add('credential_pattern')
    if re.search(r'(?i)(?:cookie|authorization)\s*:\s*(?:bearer\s+)?[a-z0-9+/=_-]{24,}',text):
        found.add('auth_header')
    return found


def audit(root):
    issues=[];files=discover(root)
    for relative in files:
        path=Path(root)/relative
        if path.is_symlink():issues.append((str(relative),'symlink'));continue
        allowed=(len(relative.parts)==1 and relative.name in ROOT_FILES) or (
            len(relative.parts)>1 and relative.parts[0] in FOLDERS and
            path.suffix in FOLDERS[relative.parts[0]])
        if not allowed:issues.append((str(relative),'unexpected_file'));continue
        if path.stat().st_size>500000:issues.append((str(relative),'oversize'));continue
        try:text=path.read_text(encoding='utf-8')
        except UnicodeError:issues.append((str(relative),'binary_or_non_utf8'));continue
        issues.extend((str(relative),category) for category in sorted(categories(text)))
    return files,issues


def main():
    root=Path(__file__).resolve().parents[1]
    files,issues=audit(root)
    if issues:
        for path,category in issues:print(category+': '+path)
        return 1
    print('Public-tree audit passed: %d text files; manual privacy review still required.' % len(files))
    return 0


if __name__=='__main__':sys.exit(main())
