import random
import re

ALPHABET = "abcdefghijklmnopqrstuvwxyz"


def _mutate_word(word, kind, rng):
    positions = [(index, char) for index, char in enumerate(word) if char.isalpha()]
    if len(positions) < 3:
        return word
    index, char = rng.choice(positions)
    if kind == "substitution":
        choices = ALPHABET.replace(char.lower(), "")
        replacement = rng.choice(choices)
        if char.isupper():
            replacement = replacement.upper()
        return word[:index] + replacement + word[index + 1:]
    if kind == "transposition":
        if index >= len(word) - 1 or not word[index + 1].isalpha():
            index, char = rng.choice(positions[:-1])
        end = index + 1
        return word[:index] + word[end] + word[index] + word[end + 1:]
    if kind == "deletion":
        return word[:index] + word[index + 1:]
    if kind == "insertion":
        letter = rng.choice(ALPHABET)
        return word[:index + 1] + letter + word[index + 1:]
    return word


def damage_text(text, settings, rng):
    text = str(text).strip()
    if not settings.get("enabled", True) or not text:
        return [text]
    probabilities = settings.get("typo_probabilities", {})
    total = sum(max(0.0, float(value)) for value in probabilities.values())
    words = list(re.finditer(r"\b[\w'-]+\b", text))
    changed = None
    output = text
    if total > 0:
        for match in reversed(words):
            if len(match.group()) < 4 or rng.random() >= min(1.0, total):
                continue
            pick = rng.random() * total
            kind = next(iter(probabilities))
            for candidate, probability in probabilities.items():
                pick -= max(0.0, float(probability))
                if pick <= 0:
                    kind = candidate
                    break
            original = match.group()
            typo = _mutate_word(original, kind, rng)
            if typo != original:
                output = output[:match.start()] + typo + output[match.end():]
                changed = original
                break
    if rng.random() < float(settings.get("lowercase_start_probability", 0)) and output:
        for index, character in enumerate(output):
            if character.isalpha():
                output = output[:index] + character.lower() + output[index + 1:]
                break
    if rng.random() < float(settings.get("drop_terminal_punctuation_probability", 0)):
        output = re.sub(r"[.!?,;:]+$", "", output)
    result = [output]
    if changed and rng.random() < float(settings.get("self_correction_probability", 0)):
        result.append("*" + changed)
    return result


def split_bursts(text, max_chars):
    text = str(text).strip()
    if not text:
        return []
    max_chars = max(20, int(max_chars))
    sentences = re.split(r"(?<=[.!?])\s+", text)
    bursts = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                bursts.append(current)
                current = ""
            words = sentence.split()
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > max_chars:
                    bursts.append(current)
                    current = word
                else:
                    current = candidate
        else:
            candidate = f"{current} {sentence}".strip()
            if current and len(candidate) > max_chars:
                bursts.append(current)
                current = sentence
            else:
                current = candidate
    if current:
        bursts.append(current)
    return bursts
