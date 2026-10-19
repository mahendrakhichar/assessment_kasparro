"""
scripts/run_all.py — execute all 4 evaluation runs.

Run order:
  1. v1 on dev  (scored)
  2. v2 on dev  (scored)
  3. v1 on check (no scoring)
  4. v2 on check (no scoring)

Usage:
  python -m scripts.run_all
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from harness.runner import run_split
from harness.report import compare_versions


if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

async def main():
    print("=" * 60)
    print("Kasparro FSE I - Running all 4 evaluation runs")
    print("=" * 60)

    # Initialize Langfuse observability if keys are provided
    langfuse_client = None
    pk = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    sk = os.getenv("LANGFUSE_SECRET_KEY", "")
    if pk and not pk.startswith("pk-lf-...") and sk and not sk.startswith("sk-lf-..."):
        try:
            from langfuse import Langfuse
            langfuse_client = Langfuse(
                public_key=pk,
                secret_key=sk,
                host=os.getenv("LANGFUSE_BASE_URL") or os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            )
            print("   [Observability] Connected to Langfuse Cloud")
        except Exception as e:
            print(f"   [Observability] Langfuse warning: {e}")

    runs = [
        ("v1", "dev"),
        ("v2", "dev"),
        ("v1", "check"),
        ("v2", "check"),
    ]

    for version, split in runs:
        print(f"\n>> Running {version} on {split}...")
        try:
            run = await run_split(version=version, split=split, langfuse_client=langfuse_client)
            print(f"   [OK] {version}_{split}: run_id={run.run_id}")
            if run.metrics:
                print(f"   Extraction F1: {run.metrics.extraction_f1:.4f}")
                print(f"   Joint F1:      {run.metrics.joint_f1:.4f}")
                print(f"   Macro F1:      {run.metrics.macro_f1:.4f}")
            else:
                print(f"   (check run - no scoring)")
        except Exception as e:
            print(f"   [FAIL] {version}_{split} FAILED: {e}")

    # Comparison report
    v1_path = Path("runs/v1_dev.json")
    v2_path = Path("runs/v2_dev.json")
    if v1_path.exists() and v2_path.exists():
        print("\n" + "=" * 60)
        print("COMPARISON REPORT")
        print("=" * 60)
        print(compare_versions(v1_path, v2_path))


if __name__ == "__main__":
    asyncio.run(main())
