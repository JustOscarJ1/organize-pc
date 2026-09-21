"""pc.py - see what is running on this PC, what every Claude lane is waiting on, and put the machine back in order.

  pc scan [--json]                inventory: lanes, notepad tabs, windows, servers, leaks. No changes.
  pc state                        write STATE.txt and lanes\*.txt from a scan, print STATE.txt.
  pc bank                         copy every unsaved Notepad tab to bank\ (never deletes).
  pc close [--apply] [--apps] [--leaks] [--sessions] [--notepad]   dry-run unless --apply.
  pc desktops [--apply]           one desktop per live lane, one for notes; Claude left, subject centre, state right.
  pc organize [--apply]           bank, state, close (all four), desktops, state again.
  pc view <file>                  a small black pane that shows and edits a state file.
  pc resume <lane|sid>            print the claude --resume command for a closed lane.

Exit codes: 0 ok, 1 something it was asked to do could not be proven, 2 bad usage.
"""
import ctypes, datetime as dt, glob, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
POLICY = json.load(open(os.path.join(HERE, "policy.json"), encoding="utf-8"))
HOME = os.path.normpath(os.path.expanduser(POLICY.get("home") or "~/pc"))
BANK = os.path.join(HOME, "bank")
LANES = os.path.join(HOME, "lanes")
STATE_TXT = os.path.join(HOME, "STATE.txt")
PROJECTS = os.path.expanduser(r"~\.claude\projects")
SESSIONS = os.path.expanduser(r"~\.claude\sessions")   # <pid>.json per live claude: sessionId, cwd, status
NOTEPAD_TABSTATE = os.path.expandvars(r"%LOCALAPPDATA%\Packages\Microsoft.WindowsNotepad_8wekyb3d8bbwe\LocalState\TabState")
BUSY_GLYPHS = "◐◑◒◓"
NOW = time.time()

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def ensure_dirs():
    for d in (HOME, BANK, LANES):
        os.makedirs(d, exist_ok=True)


def slug(s, n=60):
    s = re.sub(r"[^\w\s-]", "", s, flags=re.U).strip().lower()
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:n].strip("-") or "untitled"


def stamp(ts=None):
    return dt.datetime.fromtimestamp(ts or NOW).strftime("%d %b %Y %H:%M")


# ----------------------------------------------------------------------------- processes
def proc_table():
    import psutil
    t = {}
    for p in psutil.process_iter(["name", "ppid", "create_time", "cmdline", "memory_info", "cwd"]):
        t[p.pid] = p
    return t


def chain(t, pid, n=12):
    out = []
    while pid in t and len(out) < n:
        p = t[pid]
        out.append((pid, p.info["name"]))
        pid = p.info["ppid"]
    return out


def my_claude_pid(t):
    for pid, name in chain(t, os.getpid()):
        if name == "claude.exe":
            return pid
    return None


def console_title(pid):
    k = ctypes.windll.kernel32
    k.FreeConsole()
    title, hwnd = "", 0
    if k.AttachConsole(pid):
        buf = ctypes.create_unicode_buffer(512)
        k.GetConsoleTitleW(buf, 512)
        title = buf.value
        hwnd = k.GetConsoleWindow()
        k.FreeConsole()
    return title, hwnd


# ----------------------------------------------------------------------------- transcripts
def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def read_transcript(path):
    """First real ask, last real ask, last long assistant text, timestamps, ruling lines."""
    first = last = None
    users, asst = [], []
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if "timestamp" in o:
                if first is None:
                    first = o
                if o.get("type") in ("user", "assistant"):
                    last = o
            if o.get("type") == "user":
                c = _text_of(o.get("message", {}).get("content"))
                if c and not c.startswith("<") and not c.startswith("[") and "Base directory for this skill" not in c[:60]:
                    users.append(c)
            elif o.get("type") == "assistant":
                for p in o.get("message", {}).get("content", []):
                    if isinstance(p, dict) and p.get("type") == "text" and len(p["text"]) > 200:
                        asst.append(p["text"])

    def ts(o):
        try:
            return dt.datetime.fromisoformat(o["timestamp"].replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0

    report = asst[-1] if asst else ""
    rulings, prev = [], ""
    for l in report.splitlines():
        t = l.strip()
        if "**Ruling:**" in t or t.startswith("Ruling:"):
            q = t.replace("**Ruling:**", "").replace("Ruling:", "").strip(" :")
            rulings.append(q if len(q) > 12 else prev)
        elif t:
            prev = t.lstrip("0123456789.-* ").replace("**", "")[:160]
    return dict(first_ts=ts(first) if first else 0, last_ts=ts(last) if last else 0,
                first_ask=users[0] if users else "", last_ask=users[-1] if users else "",
                report=report, rulings=rulings, turns=len(users))


def transcripts(days=14):
    out = []
    for f in glob.glob(os.path.join(PROJECTS, "*", "*.jsonl")):
        if NOW - os.path.getmtime(f) > days * 86400:
            continue
        out.append(f)
    return out


def subagents_active(sid, minutes):
    d = os.path.join(PROJECTS, "*", sid, "subagents", "*.jsonl")
    return any(NOW - os.path.getmtime(f) < minutes * 60 for f in glob.glob(d))


# ----------------------------------------------------------------------------- windows
def list_windows():
    import psutil, win32gui, win32process
    wins = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        t = win32gui.GetWindowText(h)
        if not t:
            return
        _, pid = win32process.GetWindowThreadProcessId(h)
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = "?"
        wins.append(dict(hwnd=h, pid=pid, proc=name, title=t, min=bool(win32gui.IsIconic(h)), rect=list(win32gui.GetWindowRect(h))))

    win32gui.EnumWindows(cb, None)
    return wins


def monitors():
    import win32api
    m = {}
    for h, _, _ in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(h)
        m[info["Device"]] = info["Work"]
    return m


# ----------------------------------------------------------------------------- notepad (UIA)
_uia = None


def uia():
    global _uia
    if _uia is None:
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as U
        _uia = (comtypes.client.CreateObject(U.CUIAutomation, interface=U.IUIAutomation), U)
    return _uia


def notepad_windows():
    import win32gui, win32con, win32process, psutil
    hs = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetClassName(h) == "Notepad":
            _, pid = win32process.GetWindowThreadProcessId(h)
            if psutil.Process(pid).name() == "Notepad.exe":
                hs.append(h)

    win32gui.EnumWindows(cb, None)
    for h in hs:
        if win32gui.IsIconic(h):
            win32gui.ShowWindow(h, win32con.SW_RESTORE)
    return hs


def notepad_tabs(read_text=True):
    """Every tab of every Notepad window: name, modified flag, text. Selecting a tab is how Notepad exposes its text."""
    a, U = uia()
    out = []
    import pyvda
    home_desktop = pyvda.VirtualDesktop.current()
    for h in notepad_windows():
        try:
            d = pyvda.AppView(hwnd=h).desktop
            if d.id != pyvda.VirtualDesktop.current().id:
                d.go()
                time.sleep(0.4)
        except Exception:
            pass
        w = a.ElementFromHandle(h)
        tabs = w.FindAll(U.TreeScope_Descendants, a.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_TabItemControlTypeId))
        for j in range(tabs.Length):
            t = tabs.GetElement(j)
            name = t.CurrentName
            m = re.match(r"^(.*?)\. (Modified|Unmodified)\.?$", name)
            title, modified = (m.group(1), m.group(2) == "Modified") if m else (name, None)
            text = ""
            if read_text:
                try:
                    t.GetCurrentPattern(U.UIA_SelectionItemPatternId).QueryInterface(U.IUIAutomationSelectionItemPattern).Select()
                    time.sleep(0.3)
                    doc = w.FindFirst(U.TreeScope_Descendants, a.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_DocumentControlTypeId))
                    if doc:
                        text = doc.GetCurrentPattern(U.UIA_TextPatternId).QueryInterface(U.IUIAutomationTextPattern).DocumentRange.GetText(-1)
                except Exception as e:
                    text = f"<unreadable: {e}>"
            saved = bool(re.search(r"\.\w{1,5}$", title)) and not modified
            out.append(dict(hwnd=h, index=j, title=title, modified=modified, saved_file=saved, chars=len(text), text=text))
    try:
        if home_desktop.id != pyvda.VirtualDesktop.current().id:
            home_desktop.go()
    except Exception:
        pass
    return out


def notepad_close_tab(hwnd, index, dont_save=True):
    """Select tab, invoke its close button, answer the save dialog. Returns True when the tab is gone."""
    a, U = uia()
    try:
        import pyvda
        d = pyvda.AppView(hwnd=hwnd).desktop
        if d.id != pyvda.VirtualDesktop.current().id:
            d.go()
            time.sleep(0.4)
    except Exception:
        pass
    w = a.ElementFromHandle(hwnd)
    tabs = w.FindAll(U.TreeScope_Descendants, a.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_TabItemControlTypeId))
    if index >= tabs.Length:
        return False
    t = tabs.GetElement(index)
    name = t.CurrentName
    t.GetCurrentPattern(U.UIA_SelectionItemPatternId).QueryInterface(U.IUIAutomationSelectionItemPattern).Select()
    time.sleep(0.25)
    # the tab's close button only exists while hovered, so close with Ctrl+W on the focused window
    import win32api, win32con, win32gui
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.2)
    doc = w.FindFirst(U.TreeScope_Descendants, a.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_DocumentControlTypeId))
    if doc:
        doc.SetFocus()
        time.sleep(0.1)
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(0x57, 0, 0, 0)
    win32api.keybd_event(0x57, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    for _ in range(20):
        time.sleep(0.15)
        if not win32gui.IsWindow(hwnd):
            return True
        btns = w.FindAll(U.TreeScope_Descendants, a.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_ButtonControlTypeId))
        names = [btns.GetElement(i).CurrentName for i in range(btns.Length)]
        pick = next((n for n in names if n.lower().replace("’", "'") in ("don't save", "dont save")), None)
        if pick and dont_save:
            b = btns.GetElement(names.index(pick))
            b.GetCurrentPattern(U.UIA_InvokePatternId).QueryInterface(U.IUIAutomationInvokePattern).Invoke()
            break
        tabs2 = w.FindAll(U.TreeScope_Descendants, a.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_TabItemControlTypeId))
        if not any(tabs2.GetElement(i).CurrentName == name for i in range(tabs2.Length)):
            return True
    time.sleep(0.3)
    if not win32gui.IsWindow(hwnd):
        return True
    tabs3 = w.FindAll(U.TreeScope_Descendants, a.CreatePropertyCondition(U.UIA_ControlTypePropertyId, U.UIA_TabItemControlTypeId))
    return not any(tabs3.GetElement(i).CurrentName == name for i in range(tabs3.Length))


# ----------------------------------------------------------------------------- scan
def scan(read_notepad=True):
    import psutil
    t = proc_table()
    me = my_claude_pid(t)
    trs = transcripts()
    claude_starts = [p.info["create_time"] for p in t.values() if p.info["name"] == "claude.exe"]
    oldest_start = min(claude_starts) if claude_starts else NOW
    # every user/assistant timestamp of each transcript written since the oldest live claude started.
    # a fresh session's first entry, and a resumed session's first entry after resume, both land
    # within seconds of the process start; that is how a process finds its transcript.
    tinfo = {}
    for f in trs:
        if os.sep + "subagents" + os.sep in f or os.path.getmtime(f) < oldest_start - 60:
            continue
        stamps = []
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"timestamp"' not in line or ('"type":"user"' not in line[:200] and '"type": "user"' not in line[:200]
                                                     and '"type":"assistant"' not in line[:200] and '"type": "assistant"' not in line[:200]):
                        continue
                    try:
                        o = json.loads(line)
                        stamps.append(dt.datetime.fromisoformat(o["timestamp"].replace("Z", "+00:00")).timestamp())
                    except Exception:
                        pass
        except Exception:
            pass
        if stamps:
            tinfo[f] = dict(first_ts=stamps[0], stamps=stamps)

    lanes = []
    for pid, p in t.items():
        if p.info["name"] != "claude.exe":
            continue
        title, chwnd = console_title(pid)
        start = p.info["create_time"]
        cands = []
        reg = {}
        try:
            reg = json.load(open(os.path.join(SESSIONS, f"{pid}.json"), encoding="utf-8"))
        except Exception:
            pass
        if reg.get("sessionId"):
            hit = glob.glob(os.path.join(PROJECTS, "*", reg["sessionId"] + ".jsonl"))
            if hit:
                cands = [(0, hit[0])]
        if not cands:
            for f, v in tinfo.items():
                near = [x - start for x in v["stamps"] if -15 <= x - start <= 600]
                if near:
                    cands.append((min(abs(d) for d in near), f))
            cands.sort(key=lambda x: x[0])
        lane = dict(pid=pid, cmd_pid=p.info["ppid"], title=title.lstrip(BUSY_GLYPHS + "✳ ").strip(), raw_title=title,
                    busy=bool(title and title[0] in BUSY_GLYPHS) or reg.get("status") == "busy", start=stamp(start), age_hours=round((NOW - start) / 3600, 1),
                    registry=reg.get("status", ""),
                    rss_mb=round(p.info["memory_info"].rss / 2 ** 20),
                    sid="", transcript="", idle_hours=None, subagents=False, first_ask="", report="", rulings=[], turns=0)
        if cands:
            f = cands[0][1]
            info = read_transcript(f)
            sid = os.path.splitext(os.path.basename(f))[0]
            lane.update(sid=sid, transcript=f, idle_hours=round((NOW - info["last_ts"]) / 3600, 1),
                        subagents=subagents_active(sid, POLICY["subagent_active_minutes"]),
                        first_ask=info["first_ask"], last_ask=info["last_ask"], report=info["report"], rulings=info["rulings"], turns=info["turns"])
        young = lane["age_hours"] < POLICY["done_after_hours"]   # a process this young is someone's deliberate act
        if pid == me:
            lane["verdict"] = "self"
        elif lane["busy"] or lane["subagents"]:
            lane["verdict"] = "busy"
        elif not lane["sid"]:
            lane["verdict"] = "fresh" if young else "empty"
        elif young or lane["idle_hours"] < POLICY["done_after_hours"]:
            lane["verdict"] = "idle"
        else:
            lane["verdict"] = "done"
        lanes.append(lane)
    lanes.sort(key=lambda l: l["start"])

    # notepad
    tabs = notepad_tabs(read_text=read_notepad) if read_notepad else []
    if tabs:
        blobs = []
        own = [l["transcript"] for l in lanes if l["verdict"] == "self"]
        for f in trs:
            if f in own or os.sep + "subagents" + os.sep in f:
                continue
            try:
                with open(f, encoding="utf-8", errors="ignore") as fh:
                    parts = []
                    for line in fh:
                        if '"type":"user"' in line[:200] or '"type": "user"' in line[:200]:
                            try:
                                parts.append(_text_of(json.loads(line).get("message", {}).get("content")))
                            except Exception:
                                pass
                    blobs.append(re.sub(r"\s+", " ", "\n".join(parts)))
            except Exception:
                pass
        for tab in tabs:
            probe = re.sub(r"\s+", " ", tab["text"][:120]).strip()[:60]
            tab["sent"] = len(probe) > 25 and any(probe in b for b in blobs)
            if tab["saved_file"]:
                tab["verdict"] = "close-saved"
            elif tab["chars"] == 0:
                tab["verdict"] = "close-empty"
            elif tab["sent"]:
                tab["verdict"] = "bank-close"
            else:
                tab["verdict"] = "keep"

    # windows and apps
    wins = list_windows()
    close_apps = {n.lower() for n in POLICY["close_apps"]}
    apps = sorted({w["proc"] for w in wins if w["proc"].lower() in close_apps})
    for w in wins:
        w["verdict"] = "close" if w["proc"].lower() in close_apps else "keep"

    # servers and leaks
    live_claude = {l["pid"] for l in lanes if l["verdict"] in ("self", "busy", "idle", "fresh")}
    servers, seen = [], set()
    for c in psutil.net_connections("inet"):
        if c.status != "LISTEN" or c.laddr.port in seen or c.pid not in t:
            continue
        seen.add(c.laddr.port)
        p = t[c.pid]
        if p.info["name"] in ("svchost.exe", "System", "lsass.exe", "wininit.exe", "services.exe", "spoolsv.exe"):
            continue
        servers.append(dict(port=c.laddr.port, pid=c.pid, name=p.info["name"], cmd=" ".join(p.info["cmdline"] or [])[:140], start=stamp(p.info["create_time"])))
    leaks = []
    for pid, p in t.items():
        cmd = " ".join(p.info["cmdline"] or [])
        anc = [x for x, _ in chain(t, pid)]
        if any(a in live_claude for a in anc):
            continue
        if any(re.search(k, cmd) for k in POLICY["keep_patterns"]):
            continue
        if any(re.search(k, cmd) for k in POLICY["leak_patterns"]):
            leaks.append(dict(pid=pid, name=p.info["name"], rss_mb=round(p.info["memory_info"].rss / 2 ** 20), cmd=cmd[:120]))
    for s in servers:
        cmd = s["cmd"]
        s["verdict"] = "keep" if any(re.search(k, cmd) for k in POLICY["keep_patterns"]) else (
            "kill" if (any(re.search(k, cmd) for k in POLICY["leak_patterns"]) or s["name"].lower() in close_apps) else "keep")
    vm = psutil.virtual_memory()
    return dict(at=stamp(), me=me, lanes=lanes, notepad=tabs, windows=wins, apps=apps, servers=servers, leaks=leaks,
                ram=dict(total_gb=round(vm.total / 2 ** 30, 1), used_gb=round(vm.used / 2 ** 30, 1), pct=vm.percent))


# ----------------------------------------------------------------------------- state files
def lane_file(lane):
    return os.path.join(LANES, slug(lane["title"]) + ".txt")


def write_lane(lane, closed=False):
    ensure_dirs()
    path = lane_file(lane)
    waits = lane["rulings"]
    lines = [f"{lane['title']}", f"{'closed' if closed else lane['verdict']}  |  started {lane['start']}  |  {lane['turns']} asks  |  session {lane['sid'] or '-'}",
             f"resume: claude --resume {lane['sid']}" if lane["sid"] else "resume: nothing to resume (no transcript)", ""]
    if waits:
        lines += ["WAITS ON YOU", *[f"  {r}" for r in waits], ""]
    else:
        lines += ["WAITS ON YOU", "  nothing named in the last report", ""]
    lines += ["ASK", "  " + lane["first_ask"][:1500].replace("\n", "\n  "), "", "LAST REPORT", lane["report"][-4000:], ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


CLOSED_JSON = os.path.join(LANES, "closed.json")


def remember_closed(lanes):
    ensure_dirs()
    old = json.load(open(CLOSED_JSON, encoding="utf-8")) if os.path.exists(CLOSED_JSON) else []
    for l in lanes:
        if not l["sid"]:
            continue
        old = [o for o in old if o["sid"] != l["sid"]]
        old.append(dict(title=l["title"], sid=l["sid"], rulings=l["rulings"], report=l["report"][-300:], closed=stamp()))
    json.dump(old, open(CLOSED_JSON, "w", encoding="utf-8"), indent=1, ensure_ascii=False)


def closed_lanes_on_disk():
    return json.load(open(CLOSED_JSON, encoding="utf-8")) if os.path.exists(CLOSED_JSON) else []


def write_state(s, closed_lanes=(), notes=None):
    ensure_dirs()
    closed_lanes = closed_lanes_on_disk()
    L = [f"STATE  {s['at']}   ram {s['ram']['used_gb']}/{s['ram']['total_gb']} GB", ""]
    live = [l for l in s["lanes"] if l["verdict"] in ("busy", "idle", "self", "fresh")]
    L.append("LIVE LANES")
    for l in live:
        tag = "working" if l["busy"] or l["subagents"] else f"idle {l['idle_hours']}h"
        L.append(f"  {l['title']:<52} {tag:<14} pid {l['pid']}  {l['rss_mb']} MB")
        for r in l["rulings"][:6]:
            L.append(f"      needs you: {r[:150]}")
    L.append("")
    if closed_lanes:
        L.append("CLOSED, RESUMABLE (claude --resume <id>)")
        for l in closed_lanes:
            L.append(f"  {l['title']:<52} {l['sid']}   closed {l.get('closed', '')}")
            for r in l["rulings"][:6]:
                L.append(f"      needs you: {r[:150]}")
            if not l["rulings"] and l["report"]:
                tail = re.sub(r"\s+", " ", l["report"][-220:])
                L.append(f"      last: {tail}")
        L.append("")
    if notes is not None:
        L.append("NOTES STILL OPEN (Notes desktop)")
        for n in notes:
            L.append(f"  {n['title'][:60]:<62} {n['chars']} chars  {n['verdict']}")
        L.append("")
    L.append("SERVERS LISTENING")
    for sv in s["servers"]:
        L.append(f"  :{sv['port']:<6} {sv['name']:<22} {sv['verdict']:<5} {sv['cmd'][:90]}")
    L.append("")
    L.append(f"lanes: {LANES}\nbank:  {BANK}\nrerun: python {os.path.join(HERE, 'pc.py')} organize --apply")
    with open(STATE_TXT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    return STATE_TXT


# ----------------------------------------------------------------------------- actions
def bank_tabs(tabs):
    ensure_dirs()
    out = []
    for tab in tabs:
        if tab["saved_file"] or tab["chars"] == 0 or tab["text"].startswith("<unreadable"):
            continue
        text = tab["text"].replace("\r\n", "\n").replace("\r", "\n")
        same = [f for f in glob.glob(os.path.join(BANK, f"* {slug(tab['title'], 50)}*.txt")) if open(f, encoding="utf-8").read() == text]
        if same:
            tab["banked"] = same[0]
            out.append((tab, same[0], True))
            continue
        path = os.path.join(BANK, f"{dt.datetime.now():%Y-%m-%d %H%M} {slug(tab['title'], 50)}.txt")
        n = 2
        while os.path.exists(path):
            path = os.path.join(BANK, f"{dt.datetime.now():%Y-%m-%d %H%M} {slug(tab['title'], 50)} {n}.txt")
            n += 1
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        ok = open(path, encoding="utf-8").read() == text
        tab["banked"] = path if ok else None
        out.append((tab, path, ok))
    return out


def kill_pids(pids, why):
    import psutil
    for pid in pids:
        try:
            p = psutil.Process(pid)
            for c in p.children(recursive=True):
                try:
                    c.kill()
                except Exception:
                    pass
            p.kill()
            print(f"  killed {pid} {p.name() if p.is_running() else ''} ({why})")
        except Exception as e:
            print(f"  {pid}: {e}")


def close_apps(s, apply):
    import win32gui, win32con, psutil
    names = {a.lower() for a in s["apps"]} | {n.lower() for n in POLICY["close_apps"]}
    targets = [p for p in psutil.process_iter(["name"]) if p.info["name"] and p.info["name"].lower() in names]
    print(f"apps: {len(targets)} processes across {sorted({p.info['name'] for p in targets})}")
    if not apply:
        return
    for w in s["windows"]:
        if w["proc"].lower() in names:
            try:
                win32gui.PostMessage(w["hwnd"], win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
    time.sleep(4)
    left = [p for p in psutil.process_iter(["name"]) if p.info["name"] and p.info["name"].lower() in names]
    kill_pids([p.pid for p in left], "app")


def close_leaks(s, apply):
    print(f"leaks: {len(s['leaks'])} orphan processes, {sum(l['rss_mb'] for l in s['leaks'])} MB; servers to kill: {[sv['port'] for sv in s['servers'] if sv['verdict']=='kill']}")
    for l in s["leaks"]:
        print(f"  {l['pid']:6} {l['name']:14} {l['rss_mb']:5} MB  {l['cmd'][:90]}")
    if apply:
        kill_pids([l["pid"] for l in s["leaks"]] + [sv["pid"] for sv in s["servers"] if sv["verdict"] == "kill"], "leak")


def close_sessions(s, apply):
    gone = [l for l in s["lanes"] if l["verdict"] in ("done", "empty")]
    keep = [l for l in s["lanes"] if l["verdict"] not in ("done", "empty")]
    print(f"sessions: close {len(gone)}, keep {len(keep)}")
    for l in s["lanes"]:
        print(f"  {l['verdict']:<6} {l['pid']:6} {l['title'][:50]:<52} age {l['age_hours']}h  idle {l['idle_hours']}h  {l['sid'][:8]}")
    if not apply:
        return gone
    for l in gone:
        write_lane(l, closed=True)
    remember_closed(gone)
    kill_pids([l["pid"] for l in gone] + [l["cmd_pid"] for l in gone], "session")
    return gone


def close_notepad(s, apply):
    tabs = s["notepad"]
    todo = [t for t in tabs if t["verdict"] != "keep"]
    print(f"notepad: {len(tabs)} tabs, close {len(todo)}, keep {len(tabs) - len(todo)}")
    for t in tabs:
        print(f"  {t['verdict']:<12} {t['chars']:6}  {t['title'][:60]}")
    if not apply:
        return
    banked = bank_tabs([t for t in todo if t["verdict"] == "bank-close"])
    bad = [t["title"] for t, _, ok in banked if not ok]
    if bad:
        print("  bank read-back failed, not closing:", bad)
    # close from the highest index down per window so indices stay valid
    for t in sorted(todo, key=lambda x: (x["hwnd"], -x["index"])):
        if t["verdict"] == "bank-close" and not t.get("banked"):
            continue
        ok = notepad_close_tab(t["hwnd"], t["index"])
        print(f"  {'closed' if ok else 'STILL OPEN'}  {t['title'][:60]}")


# ----------------------------------------------------------------------------- desktops
def place(hwnd, mon, cols=1, col=0):
    import win32gui, win32con
    l, t, r, b = mon
    w = (r - l) // cols
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetWindowPos(hwnd, 0, l + col * w, t, w, b - t, win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)


def open_view(path, rect=None, label=""):
    """Spawn a view pane. New windows open on the current desktop, so switch first; tk sets its own geometry.
    The title carries the desktop label so two panes on one file stay distinguishable."""
    import win32gui
    args = [sys.executable, os.path.abspath(__file__), "view", path]
    l, t, r, b = rect if rect else (100, 100, 900, 700)
    args += [f"{r - l - 16}x{b - t - 60}+{l}+{t}", label]
    subprocess.Popen(args, creationflags=0x00000008)
    want = "pc: " + os.path.splitext(os.path.basename(path))[0] + (f" ({label})" if label else "")
    for _ in range(40):
        time.sleep(0.15)
        h = win32gui.FindWindow(None, want)
        if h:
            return h
    return 0


def close_views():
    """Kill every pc view pane so a re-run does not stack duplicates."""
    import win32gui, win32process, psutil
    pids = set()

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32gui.GetWindowText(h).startswith("pc: "):
            pids.add(win32process.GetWindowThreadProcessId(h)[1])

    win32gui.EnumWindows(cb, None)
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except Exception:
            pass
    if pids:
        time.sleep(0.5)


def move_to(hwnd, d):
    """Move a window to a desktop; a window that vanished mid-run is not an error."""
    import pyvda, win32gui
    try:
        pyvda.AppView(hwnd=hwnd).move(d)
        return True
    except Exception as e:
        print(f"  could not move {win32gui.GetWindowText(hwnd)[:50]!r} to {d.name}: {e}")
        return False


def desktop_of(hwnd):
    import pyvda
    try:
        return pyvda.AppView(hwnd=hwnd).desktop.id
    except Exception:
        return None


def desktops(s, apply, notes=None):
    import pyvda
    mons = monitors()
    M = {k: mons.get(v) for k, v in POLICY.get("monitors", {}).items()}
    if not all(M.values()):
        prim = next(iter(mons.values()))
        l, t, r, b = prim
        w = (r - l) // 3
        M = dict(left=(l, t, l + w, b), centre=(l + w, t, l + 2 * w, b), right=(l + 2 * w, t, r, b))
    live = [l for l in s["lanes"] if l["verdict"] in ("self", "busy", "idle", "fresh")]
    me = [l for l in live if l["verdict"] == "self"]
    others = [l for l in live if l["verdict"] != "self"]
    plan = [("Now", me)] + [(l["title"][:40], [l]) for l in others] + [("Notes", [])]
    print("desktops:")
    for name, ls in plan:
        print(f"  {name:<42} {', '.join(str(l['pid']) for l in ls) or 'notepad + STATE'}")
    if not apply:
        return
    close_views()
    existing = {d.name: d for d in pyvda.get_virtual_desktops()}
    con_hwnd = {}
    for l in live:
        raw = console_title(l["pid"])[0].lstrip(BUSY_GLYPHS + "✳ ").strip()
        for w in s["windows"]:
            if w["proc"] == "WindowsTerminal.exe" and w["title"].lstrip(BUSY_GLYPHS + "✳ ").strip() == raw:
                con_hwnd[l["pid"]] = w["hwnd"]
                break
    title_words = lambda t: {w.lower() for w in re.findall(r"[A-Za-z]{4,}", t)}
    used = set()
    for i, (name, ls) in enumerate(plan):
        d = existing.get(name)
        if d is None:
            d = pyvda.get_virtual_desktops()[0] if i == 0 else pyvda.VirtualDesktop.create()
            d.rename(name)
        d.go()
        time.sleep(0.5)
        for l in ls:
            h = con_hwnd.get(l["pid"])
            if h:
                move_to(h, d)
                place(h, M["left"])
                used.add(h)
            for w in s["windows"]:
                if w["hwnd"] in used or w["proc"] in ("WindowsTerminal.exe", "Notepad.exe", "explorer.exe", "python.exe") or w["verdict"] == "close":
                    continue
                if len(title_words(w["title"]) & title_words(l["title"])) >= 2:
                    move_to(w["hwnd"], d)
                    place(w["hwnd"], M["centre"])
                    used.add(w["hwnd"])
            open_view(write_lane(l), M["right"], name)
        if name == "Now":
            open_view(STATE_TXT, M["centre"], name)
        if name == "Notes":
            for k, h in enumerate(notepad_windows()):
                move_to(h, d)
                place(h, M["left"] if k % 2 == 0 else M["centre"])
            open_view(STATE_TXT, M["right"], name)
    names = {n for n, _ in plan}
    for d in pyvda.get_virtual_desktops():
        if d.name and d.name not in names and not any(desktop_of(w["hwnd"]) == d.id for w in s["windows"] if w["verdict"] != "close"):
            d.remove()
    pyvda.VirtualDesktop(1).go()


# ----------------------------------------------------------------------------- view pane
def view(path, geometry=None, label=""):
    import tkinter as tk
    root = tk.Tk()
    if geometry:
        root.geometry(geometry)
    name = os.path.splitext(os.path.basename(path))[0]
    root.title("pc: " + name + (f" ({label})" if label else ""))
    root.configure(bg="black")
    txt = tk.Text(root, bg="black", fg="white", insertbackground="white", font=("Consolas", 11), wrap="word", bd=0, padx=14, pady=12)
    txt.pack(fill="both", expand=True)
    txt.tag_configure("red", foreground="#ff3b3b")
    txt.tag_configure("green", foreground="#2ecc71")
    state = dict(mtime=0, dirty=False)

    def paint():
        for tag in ("red", "green"):
            txt.tag_remove(tag, "1.0", "end")
        for i, line in enumerate(txt.get("1.0", "end").splitlines(), 1):
            if "needs you" in line or "WAITS ON YOU" in line or "**Ruling:**" in line or "STILL OPEN" in line:
                txt.tag_add("red", f"{i}.0", f"{i}.end")
            elif line.startswith(("LIVE LANES", "STATE", "CLOSED", "NOTES", "SERVERS", "LAST REPORT", "ASK", "resume:")) or "working" in line:
                txt.tag_add("green", f"{i}.0", f"{i}.end")

    def load():
        try:
            m = os.path.getmtime(path)
        except OSError:
            return
        if m != state["mtime"] and not state["dirty"]:
            state["mtime"] = m
            txt.delete("1.0", "end")
            txt.insert("1.0", open(path, encoding="utf-8").read())
            paint()
        root.after(2000, load)

    def save(_=None):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(txt.get("1.0", "end-1c"))
        state["dirty"] = False
        state["mtime"] = os.path.getmtime(path)
        paint()
        return "break"

    txt.bind("<Control-s>", save)
    txt.bind("<Key>", lambda e: state.update(dirty=True) if e.char else None)
    load()
    root.mainloop()


# ----------------------------------------------------------------------------- main
def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    cmd, flags = argv[0], set(argv[1:])
    apply = "--apply" in flags
    if cmd == "view":
        view(argv[1], argv[2] if len(argv) > 2 else None, argv[3] if len(argv) > 3 else "")
        return 0
    if cmd == "resume":
        key = argv[1].lower()
        for f in glob.glob(os.path.join(LANES, "*.txt")):
            body = open(f, encoding="utf-8").read()
            if key in os.path.basename(f).lower() or key in body[:400].lower():
                m = re.search(r"resume: (.*)", body)
                print(m.group(1) if m else "no session id recorded")
                return 0
        print("no lane matched")
        return 1
    print("scanning ...", flush=True)
    s = scan(read_notepad=cmd in ("scan", "organize", "close", "bank", "state") and "--no-notepad" not in flags)
    if cmd == "scan":
        if "--json" in flags:
            print(json.dumps({k: v for k, v in s.items() if k != "windows"}, indent=1, default=str, ensure_ascii=False))
            return 0
        print(f"\n{s['at']}  ram {s['ram']['used_gb']}/{s['ram']['total_gb']} GB\n\nLANES")
        for l in s["lanes"]:
            print(f"  {l['verdict']:<6} {l['pid']:6} {l['rss_mb']:4} MB  {l['title'][:48]:<50} age {l['age_hours']}h  idle {l['idle_hours']}h  sub {'y' if l['subagents'] else 'n'}  {l['sid'][:8]}")
        print("\nNOTEPAD")
        for t in s["notepad"]:
            print(f"  {t['verdict']:<12} {t['chars']:6}  {t['title'][:60]}")
        print("\nAPPS TO CLOSE:", ", ".join(s["apps"]) or "none")
        print("\nSERVERS")
        for sv in s["servers"]:
            print(f"  :{sv['port']:<6} {sv['verdict']:<5} {sv['name']:<22} {sv['cmd'][:80]}")
        print(f"\nLEAKS: {len(s['leaks'])} procs, {sum(l['rss_mb'] for l in s['leaks'])} MB")
        return 0
    if cmd == "bank":
        for tab, path, ok in bank_tabs(s["notepad"]):
            print(f"  {'ok ' if ok else 'BAD'} {path}")
        return 0
    if cmd == "state":
        for l in s["lanes"]:
            if l["sid"]:
                write_lane(l)
        p = write_state(s, notes=[t for t in s["notepad"] if t["verdict"] == "keep"])
        print(open(p, encoding="utf-8").read())
        return 0
    if cmd in ("close", "organize"):
        which = {f for f in ("--apps", "--leaks", "--sessions", "--notepad") if f in flags} or {"--apps", "--leaks", "--sessions", "--notepad"}
        closed = []
        if cmd == "organize":
            for tab, path, ok in bank_tabs(s["notepad"]):
                print(f"  banked {'ok ' if ok else 'BAD'} {path}")
        if "--sessions" in which:
            closed = close_sessions(s, apply)
        if "--notepad" in which:
            close_notepad(s, apply)
        if "--apps" in which:
            close_apps(s, apply)
        if "--leaks" in which:
            close_leaks(s, apply)
        if cmd == "close":
            print("\n(dry run; add --apply)" if not apply else "\ndone")
            return 0
        if apply:
            time.sleep(2)
            s = scan(read_notepad=True)
        keep_notes = [t for t in s["notepad"] if t["verdict"] == "keep"] if apply else [t for t in s["notepad"] if t["verdict"] == "keep"]
        for l in s["lanes"]:
            if l["sid"]:
                write_lane(l)
        write_state(s, closed_lanes=closed, notes=keep_notes)
        desktops(s, apply, notes=keep_notes)
        write_state(s, closed_lanes=closed, notes=keep_notes)
        print(open(STATE_TXT, encoding="utf-8").read() if apply else "\n(dry run; add --apply)")
        return 0
    if cmd == "desktops":
        desktops(s, apply)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
