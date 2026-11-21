"""One process that imports every sqlglot module of the instrumented tree and writes,
for each class-level dict literal, the runtime key of each explicit entry, plus the
classes whose attributes are not plain class attributes (enums, named tuples,
dataclasses). usage: keys_run.py INSTR_SRC OUT.json"""
import enum, importlib, json, os, pkgutil, sys

def main():
    src, out = sys.argv[1], sys.argv[2]
    os.chdir(src); sys.path.insert(0, src)
    import sqlglot, _brrec
    failed = []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(m.name)
        except BaseException as e:
            failed.append((m.name, repr(e)[:100]))
    special = []
    for name, mod in list(sys.modules.items()):
        if not name.startswith("sqlglot"):
            continue
        stack = [v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == name]
        seen = set()
        while stack:
            c = stack.pop()
            if c in seen: continue
            seen.add(c)
            if isinstance(c, enum.EnumMeta) or hasattr(c, "__dataclass_fields__") or (issubclass(c, tuple) and hasattr(c, "_fields")):
                special.append(f"{c.__module__}:{c.__qualname__}")
            stack.extend(v for v in vars(c).values() if isinstance(v, type) and v.__module__ == name)
    json.dump({"keys": _brrec.KEYS, "special": sorted(special), "failed": failed}, open(out, "w"))
    print("sites", len(_brrec.KEYS), "special classes", len(special), "failed imports", failed)

if __name__ == "__main__":
    main()
