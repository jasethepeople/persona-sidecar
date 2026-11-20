import math
import re


def cumulative_schedule(bursts, timing, rng, now):
    reading_wpm = float(timing["reading_wpm"])
    median = max(0.05, float(timing["latency_median_seconds"]))
    sigma = max(0.0, float(timing["latency_sigma"]))
    cursor = float(now)
    scheduled = []
    for index, text in enumerate(bursts):
        words = len(re.findall(r"\b[\w']+\b", text))
        reading_seconds = words * 60.0 / reading_wpm
        burst_latency = rng.lognormvariate(math.log(median), sigma)
        cursor += max(0.05, reading_seconds + burst_latency)
        scheduled.append({"burst_index": index, "text": text, "due_at": cursor})
    return scheduled
