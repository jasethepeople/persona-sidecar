import json
import re
import urllib.request

from .affect import LABEL_DELTAS


def _contains_phrase(text, phrase):
    return re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text) is not None


class RulesClassifier:
    _patterns = {
        "hostile": ("hate", "shut up", "idiot", "go to hell"),
        "conflict": ("disagree", "arguing", "upset", "not okay"),
        "struggling": ("overwhelmed", "can't cope", "cannot cope", "exhausted", "rough day"),
        "vulnerable": ("scared", "lonely", "afraid", "feel lost", "miss you", "vulnerable"),
        "reassuring": ("can do this", "believe in you", "you are safe", "got you", "proud of you"),
        "affectionate": ("love", "care about you", "sweetheart", "thank you for being here"),
        "playful": ("haha", "lol", "just kidding", "joking", "😉"),
        "distant": ("leave me alone", "don't care", "do not care", "whatever", "i'm fine"),
    }
    _arousal = {
        "hostile": 0.95, "conflict": 0.8, "struggling": 0.75,
        "vulnerable": 0.65, "reassuring": 0.45, "affectionate": 0.55,
        "playful": 0.7, "distant": 0.3, "neutral": 0.5,
    }

    def classify(self, text):
        normalized = re.sub(r"\s+", " ", str(text).casefold()).strip()
        scores = []
        for priority, (label, phrases) in enumerate(self._patterns.items()):
            score = sum(2 if " " in phrase else 1 for phrase in phrases if _contains_phrase(normalized, phrase))
            if score:
                scores.append((score, -priority, label))
        label = max(scores)[2] if scores else "neutral"
        return label, self._arousal[label]


class OllamaClassifier:
    def __init__(self, settings):
        self.endpoint = settings["endpoint"]
        self.model = settings["model"]
        self.timeout = float(settings["timeout_seconds"])

    def classify(self, text):
        labels = ", ".join(sorted(LABEL_DELTAS))
        prompt = (
            "Classify this message's tone. Choose one label: " + labels + ". "
            "Return JSON only with label and arousal (0 to 1). Do not infer identity or intent.\nMessage:\n" + text
        )
        payload = json.dumps({
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=self.timeout) as response:
            result = json.loads(response.read(1024 * 1024).decode("utf-8"))
        content = result.get("message", {}).get("content", "")
        parsed = json.loads(content)
        label = parsed.get("label")
        if label not in LABEL_DELTAS:
            raise ValueError("classifier returned an unknown label")
        arousal = float(parsed.get("arousal", 0.5))
        if not 0 <= arousal <= 1:
            raise ValueError("classifier returned arousal outside [0, 1]")
        return label, arousal


def create_classifier(settings):
    backend = settings.get("backend", "rules")
    if backend == "rules":
        return RulesClassifier()
    if backend == "ollama":
        return OllamaClassifier(settings)
    raise ValueError("classifier.backend must be 'rules' or 'ollama'")
