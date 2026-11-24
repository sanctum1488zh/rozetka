"""Load keys from a KEY=VALUE file into os.environ (never prints values)."""
import os, re, sys

def load(path):
    t = open(path, "rb").read().decode("utf-8-sig", errors="replace")
    found = []
    for line in t.splitlines():
        m = re.match(r"\s*([A-Z_]+)\s*=\s*['\"]?([^'\"\s]+)['\"]?\s*$", line)
        if m and m.group(1).endswith("_API_KEY"):
            os.environ[m.group(1)] = m.group(2)
            found.append(m.group(1))
    return found

if __name__ == "__main__":
    print(load(sys.argv[1]))
