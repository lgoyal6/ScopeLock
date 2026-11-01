"""Export the bounded standard-library policy workflow, without voice credentials."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument("--out",type=Path,required=True)
args=parser.parse_args()
if args.out.exists() and any(args.out.iterdir()):
    raise SystemExit("Output directory must be empty")
args.out.mkdir(parents=True,exist_ok=True)
files=["scopelock/__init__.py","scopelock/agents/__init__.py","scopelock/agents/scripts.py","scopelock/core/__init__.py"]
files += ["scopelock/core/"+name+".py" for name in ("db","audit","policy","mandate","redact","intent_guard","consult","relay")]
manifest={}
for name in files:
    dst=args.out/name;dst.parent.mkdir(parents=True,exist_ok=True);data=(ROOT/name).read_bytes();dst.write_bytes(data);manifest[name]=hashlib.sha256(data).hexdigest()
for src in (ROOT/"public-demo").rglob("*"):
    if src.is_file() and "__pycache__" not in src.parts and src.suffix!=".pyc" and src.name!="README.md":
        name=src.relative_to(ROOT/"public-demo");dst=args.out/name;dst.parent.mkdir(parents=True,exist_ok=True);data=src.read_bytes();dst.write_bytes(data);manifest[str(name)]=hashlib.sha256(data).hexdigest()
(args.out/"requirements.txt").write_text("")
(args.out/".python-version").write_text("3.12\n")
(args.out/"source-manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
print("Exported",len(manifest),"source files; stdlib-only, no Guava voice runtime")
