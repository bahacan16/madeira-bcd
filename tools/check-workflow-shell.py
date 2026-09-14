#!/usr/bin/env python3
"""bash -n every run: block in the workflows.

A shell syntax error costs a whole macOS run to discover -- twenty minutes
and ten times the billed minutes -- and the log buries it under the
chain of steps that failed afterwards for want of its output. This finds
it in a second. Run it before pushing a workflow change.
"""
import os, subprocess, sys, tempfile, yaml

bad = total = 0
for wf in sorted(os.listdir(".github/workflows")):
    if not wf.endswith((".yml", ".yaml")):
        continue
    doc = yaml.safe_load(open(os.path.join(".github/workflows", wf)))
    for job in (doc.get("jobs") or {}).values():
        for i, step in enumerate(job.get("steps", [])):
            script = step.get("run")
            if not script:
                continue
            total += 1
            with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
                f.write(script)
                path = f.name
            proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
            os.unlink(path)
            if proc.returncode:
                bad += 1
                print(f"{wf}: step {i} -- {step.get('name', '(unnamed)')}")
                print(proc.stderr.replace(path, "  "))

print(f"checked {total} run blocks, {bad} with syntax errors")
sys.exit(1 if bad else 0)
