import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from persona_sidecar.classifier import create_classifier
from persona_sidecar.config import DEFAULT_CONFIG


def run(backend, samples, endpoint, model, timeout):
    settings = dict(DEFAULT_CONFIG["classifier"])
    settings.update({"backend": backend, "endpoint": endpoint, "model": model, "timeout_seconds": timeout})
    classifier = create_classifier(settings)
    latencies = []
    correct = 0
    cold = None
    for item in samples:
        started = time.perf_counter()
        predicted, _ = classifier.classify(item["text"])
        elapsed = time.perf_counter() - started
        if cold is None:
            cold = elapsed
        latencies.append(elapsed)
        correct += predicted == item["label"]
    warm = latencies[1:] or latencies
    return {
        "backend": backend,
        "model": model if backend == "ollama" else None,
        "samples": len(samples),
        "accuracy": correct / len(samples),
        "correct": correct,
        "cold_seconds": cold,
        "warm_mean_seconds": statistics.mean(warm),
        "warm_median_seconds": statistics.median(warm),
        "warm_p95_seconds": sorted(warm)[max(0, int(len(warm) * 0.95 + 0.999999) - 1)],
    }


def main():
    parser = argparse.ArgumentParser(description="Measure classifier accuracy and cold/warm latency on labelled local fixtures")
    parser.add_argument("--backend", choices=("rules", "ollama", "both"), default="rules")
    parser.add_argument("--endpoint", default=DEFAULT_CONFIG["classifier"]["endpoint"])
    parser.add_argument("--model", default=DEFAULT_CONFIG["classifier"]["model"])
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_CONFIG["classifier"]["timeout_seconds"])
    parser.add_argument("--fixtures", type=Path, default=ROOT / "tests" / "fixtures" / "classifier_samples.json")
    args = parser.parse_args()
    samples = json.loads(args.fixtures.read_text(encoding="utf-8"))
    backends = ("rules", "ollama") if args.backend == "both" else (args.backend,)
    results = []
    for backend in backends:
        try:
            results.append(run(backend, samples, args.endpoint, args.model, args.timeout_seconds))
        except Exception as exc:
            results.append({"backend": backend, "error": type(exc).__name__, "message": str(exc)[:240]})
    print(json.dumps({"fixture_file": str(args.fixtures), "results": results}, indent=2))
    return 1 if any("error" in result for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
