#!/usr/bin/env python3
"""A stdlib-only training job fixture for demonstrating Babysit's pause and recovery flow."""
import argparse
import math
import re
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="Demo job for Babysit. Checkpoints are fixture files.")
    parser.add_argument("--lr", type=float, default=0.02)
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--resume")
    parser.add_argument("--log", default="train_demo.log")
    parser.add_argument("--ckpt-dir", default="checkpoints")
    args = parser.parse_args()
    if not math.isfinite(args.lr) or args.lr <= 0 or args.steps <= 0:
        parser.error("--lr and --steps must be positive finite values")
    start = 0
    if args.resume:
        match = re.search(r"(\d+)", Path(args.resume).name)
        if not match:
            parser.error("--resume filename must contain its checkpoint step")
        start = int(match.group(1))
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_dir = Path(args.ckpt_dir)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    loss = 1.0
    with log_path.open("a", encoding="utf-8", buffering=1) as log:
        for step in range(start + 1, args.steps + 1):
            if args.lr > 0.01 and step >= 60:
                loss_text = "nan"
            else:
                loss *= 1 - min(args.lr * 4, 0.08)
                loss_text = f"{loss:.6f}"
            line = f"step {step}/{args.steps} loss={loss_text} lr={args.lr:g}"
            print(line, flush=True)
            log.write(line + "\n")
            if step % 20 == 0:
                (checkpoint_dir / f"ckpt_step{step}.npz").write_bytes(b"babysit demo checkpoint\n")
            time.sleep(0.25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
