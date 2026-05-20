import json
from pathlib import Path

class Memory:
    def __init__(self, path: str = "memory.json"):
        self.path = Path(path)
        self.data = {}
        self.load()

    def load(self):
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        else:
            self.data = {}

    def save(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def add(self, key: str, value):
        self.data[key] = value
        self.save()

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def delete(self, key: str):
        if key in self.data:
            del self.data[key]
            self.save()

    def list_keys(self):
        return list(self.data.keys())