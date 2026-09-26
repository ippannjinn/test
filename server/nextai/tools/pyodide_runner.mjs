// NextAI code sandbox: CPython (Pyodide/WebAssembly) inside Deno.
// Deno is started with --allow-read=<pyodide>,<workdir> --allow-write=<workdir> and nothing else: no network,
// no subprocesses, no env, no FFI. Python only sees an in-memory copy of the workspace; files it creates or
// changes are written back when it finishes.
// usage: deno run <perms> pyodide_runner.mjs <pyodide_dir> <workdir> <max_file_mb>
const [pyodideDir, workdir, maxFileMb] = Deno.args;
const MAX_FILE = Number(maxFileMb || 50) * 1024 * 1024;
const enc = new TextEncoder();
const out = (s) => Deno.stdout.writeSync(enc.encode(s));
const err = (s) => Deno.stderr.writeSync(enc.encode(s));
const sep = (p) => p.replace(/\\/g, "/").replace(/\/+$/, "");
const root = sep(workdir);
const skip = new Set(["__nextai_run__.py"]);

function* walk(dir, rel = "") {
  for (const e of Deno.readDirSync(dir)) {
    const r = rel ? `${rel}/${e.name}` : e.name;
    if (e.isDirectory) yield* walk(`${dir}/${e.name}`, r);
    else if (e.isFile) yield r;
  }
}

const { loadPyodide } = await import(new URL(`file:///${sep(pyodideDir).replace(/^\/+/, "")}/pyodide.mjs`).href);
const py = await loadPyodide({
  indexURL: sep(pyodideDir) + "/",
  env: { HOME: "/workspace", MPLBACKEND: "Agg", PYTHONIOENCODING: "utf-8" },
  stdout: (s) => out(s + "\n"),
  stderr: (s) => err(s + "\n"),
});
const FS = py.FS;
FS.mkdirTree("/workspace");
const before = new Map();
for (const rel of walk(root)) {
  const st = Deno.statSync(`${root}/${rel}`);
  if (st.size > MAX_FILE || skip.has(rel)) continue;
  const dir = rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : "";
  if (dir) FS.mkdirTree(`/workspace/${dir}`);
  FS.writeFile(`/workspace/${rel}`, Deno.readFileSync(`${root}/${rel}`));
  before.set(rel, FS.stat(`/workspace/${rel}`).mtime.getTime());
}

const code = new TextDecoder().decode(FS.readFile("/workspace/main.py"));
try {
  // only packages that were installed next to Pyodide load; anything else is reported by Python as usual
  // a package that is not installed would need the network: stay quiet, Python reports ModuleNotFoundError
  const quiet = (m) => /net access|following error occurred while loading/i.test(String(m));
  await py.loadPackagesFromImports(code, { messageCallback: () => {}, errorCallback: (m) => { if (!quiet(m)) err(`[package] ${m}\n`); } });
} catch (e) {
  if (!quiet(e.message || e)) err(`[package] ${String(e.message || e).split("\n")[0]}\n`);
}

let exitCode = 0;
try {
  exitCode = await py.runPythonAsync(`
import os, sys, runpy, traceback
os.chdir('/workspace')
sys.argv = ['main.py']
sys.path.insert(0, '/workspace')
_code = 0
try:
    runpy.run_path('main.py', run_name='__main__')
except SystemExit as e:
    _code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    if not isinstance(e.code, (int, type(None))):
        print(e.code, file=sys.stderr)
except BaseException as _e:
    # hide the runner's own frames: the traceback starts at the user's code
    _tb = _e.__traceback__
    while _tb is not None and not _tb.tb_frame.f_code.co_filename.endswith('main.py'):
        _tb = _tb.tb_next
    traceback.print_exception(type(_e), _e, _tb)
    _code = 1
# plt.show() does nothing without a screen: keep unsaved figures as PNG files instead
if 'matplotlib.pyplot' in sys.modules:
    try:
        _plt = sys.modules['matplotlib.pyplot']
        for _i, _n in enumerate(_plt.get_fignums(), 1):
            _name = 'figure.png' if len(_plt.get_fignums()) == 1 else f'figure_{_i}.png'
            if not os.path.exists(_name):
                _plt.figure(_n).savefig(_name, dpi=120, bbox_inches='tight')
    except Exception as _e:
        print('figure save failed:', _e, file=sys.stderr)
sys.stdout.flush(); sys.stderr.flush()
_code
`);
} catch (e) {
  err(String(e.message || e) + "\n");
  exitCode = 1;
}

function* walkFs(dir, rel = "") {
  for (const name of FS.readdir(dir)) {
    if (name === "." || name === "..") continue;
    const p = `${dir}/${name}`;
    const r = rel ? `${rel}/${name}` : name;
    const st = FS.stat(p);
    if (FS.isDir(st.mode)) yield* walkFs(p, r);
    else if (FS.isFile(st.mode)) yield [r, st];
  }
}
for (const [rel, st] of walkFs("/workspace")) {
  if (skip.has(rel) || rel.startsWith("__pycache__") || rel.includes("/__pycache__/")) continue;
  if (before.get(rel) === st.mtime.getTime()) continue;
  if (st.size > MAX_FILE) { err(`[file] ${rel} は大きすぎるため保存しませんでした\n`); continue; }
  const dir = rel.includes("/") ? rel.slice(0, rel.lastIndexOf("/")) : "";
  if (dir) Deno.mkdirSync(`${root}/${dir}`, { recursive: true });
  Deno.writeFileSync(`${root}/${rel}`, FS.readFile(`/workspace/${rel}`));
}
Deno.exit(typeof exitCode === "number" ? exitCode : 0);
