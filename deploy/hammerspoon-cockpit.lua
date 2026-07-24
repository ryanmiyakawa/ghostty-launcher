-- >>> agent-cockpit
-- Agent Cockpit: focus a terminal window by title (Ghostty + iTerm2).
-- The dashboard (ghostty_dashboard.py) proxies /api/focus?title=…&alt=…&hint=…
-- to this local HTTP server. We match a terminal window whose title contains
-- the given needle (case-insensitive), trying title → alt → hint in order,
-- and raise the first match.
--
-- Install: append this whole block (delimiters included) to ~/.hammerspoon/init.lua
-- then reload Hammerspoon (`hs -c "hs.reload()"` or relaunch the app).
-- Requires Hammerspoon's Accessibility permission to focus/raise windows.
require("hs.ipc")
hs.ipc.cliInstall()  -- makes the `hs` CLI work (no-op if already installed)

-- Terminal apps to scan. Ghostty runs one process per window (so we iterate
-- every instance for its bundle id); iTerm2 is a single process but the same
-- iteration handles it fine.
local cockpitTerminalBundles = {
    "com.mitchellh.ghostty",
    "com.googlecode.iterm2",
}

local function cockpitUrlDecode(s)
    return s:gsub("+", " "):gsub("%%(%x%x)", function(h)
        return string.char(tonumber(h, 16))
    end)
end

-- Resolved HostName for an ssh alias/target (via `ssh -G`), lower-cased.
-- Lets us match interactive ssh processes to a host by the box they actually
-- reach rather than by a literal alias substring (aliases for the same box
-- differ: `ap3-gateway`, the raw `user@host`, etc.). Empty on any failure.
local function cockpitSshHostname(name)
    if not name or name == "" then return "" end
    local out = hs.execute("/usr/bin/ssh -G " .. name .. " 2>/dev/null") or ""
    return (out:match("hostname%s+(%S+)") or ""):lower()
end

local function cockpitFocusTerminal(needle)
    if not needle or needle == "" then return false end
    needle = needle:lower()
    local sawAny = false
    for _, bundle in ipairs(cockpitTerminalBundles) do
        local apps = hs.application.applicationsForBundleID(bundle) or {}
        if #apps > 0 then sawAny = true end
        for _, app in ipairs(apps) do
            for _, w in ipairs(app:allWindows()) do
                local wt = (w:title() or ""):lower()
                if wt ~= "" and wt:find(needle, 1, true) then
                    w:focus()
                    app:activate(true)
                    return true
                end
            end
        end
    end
    -- Fallback if no bundle lookup returned anything: scan every window on
    -- screen and match by owning-app name.
    if not sawAny then
        for _, w in ipairs(hs.window.allWindows()) do
            local app = w:application()
            local an = app and (app:name() or ""):lower() or ""
            local wt = (w:title() or ""):lower()
            if (an:find("ghostty", 1, true) or an:find("iterm", 1, true))
                    and wt ~= "" and wt:find(needle, 1, true) then
                w:focus()
                if app then app:activate(true) end
                return true
            end
        end
    end
    -- iTerm2 groups sessions in tabs, and the window title only shows the
    -- ACTIVE tab — a matching session in a background tab is invisible to the
    -- window scan above. Ask iTerm2 (AppleScript) to search every session of
    -- every tab and select the match. Only when iTerm2 is already running, so
    -- the `tell` can't launch it.
    local iterm = hs.application.applicationsForBundleID("com.googlecode.iterm2") or {}
    if #iterm > 0 then
        local esc = needle:gsub("\\", "\\\\"):gsub('"', '\\"')
        local ok, result = hs.osascript.applescript([[
            tell application "iTerm2"
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if (name of s) contains "]] .. esc .. [[" then
                                select t
                                select w
                                activate
                                return "1"
                            end if
                        end repeat
                    end repeat
                end repeat
            end tell
            return "0"
        ]])
        if ok and result == "1" then
            for _, app in ipairs(iterm) do app:activate(true) end
            return true
        end
    end
    return false
end

-- Focus the terminal window that OWNS an interactive `ssh <target>` process.
-- Deterministic join for remote sessions living in locked-title windows (the
-- title never shows the remote ai-title, so title matching can't find them):
-- find the ssh client, then (Ghostty: one process per window) walk its
-- ancestry to the owning ghostty pid and focus that app's window, or
-- (iTerm2: single process) match the ssh client's tty to a session tty via
-- AppleScript and select that tab.
local function cockpitFocusBySsh(target)
    if not target or target == "" then return false end
    local wantHost = cockpitSshHostname(target)   -- box `target` actually reaches
    local out = hs.execute("ps -axo pid=,command= | grep -E '[s]sh ' 2>/dev/null") or ""
    for line in out:gmatch("[^\n]+") do
        local pid, cmd = line:match("^%s*(%d+)%s+(.*)$")
        -- An interactive ssh whose target reaches the SAME box: match either by
        -- literal substring (fast path for identical aliases) or, when that
        -- misses, by resolving each destination-looking token with `ssh -G` and
        -- comparing HostName. Tunnels (-N) are always excluded.
        local matches = false
        if pid and cmd and cmd:find("ssh", 1, true) and not cmd:find("-N", 1, true) then
            if cmd:find(target, 1, true) then
                matches = true
            elseif wantHost ~= "" then
                for tok in cmd:gmatch("%S+") do
                    -- skip flags, the `ssh` word itself, and path/opt tokens
                    -- (e.g. the ghostty binary path) to avoid needless resolves
                    if tok ~= "ssh" and tok:sub(1, 1) ~= "-"
                            and not tok:find("/", 1, true)
                            and cockpitSshHostname(tok) == wantHost then
                        matches = true
                        break
                    end
                end
            end
        end
        if matches then
            pid = tonumber(pid)
            -- Ghostty: ancestor walk to the per-window ghostty process.
            local p = pid
            for _ = 1, 8 do
                local pp = tonumber((hs.execute("ps -o ppid= -p " .. p) or ""):match("%d+") or "")
                if not pp or pp <= 1 then break end
                local comm = (hs.execute("ps -o comm= -p " .. pp) or ""):lower()
                if comm:find("ghostty", 1, true) then
                    local apps = hs.application.applicationsForBundleID("com.mitchellh.ghostty") or {}
                    for _, app in ipairs(apps) do
                        if app:pid() == pp then
                            local ws = app:allWindows()
                            if #ws > 0 then
                                ws[1]:focus()
                                app:activate(true)
                                return true
                            end
                        end
                    end
                end
                p = pp
            end
            -- iTerm2: join by tty.
            local tty = (hs.execute("ps -o tty= -p " .. pid) or ""):gsub("%s+", "")
            local iterm = hs.application.applicationsForBundleID("com.googlecode.iterm2") or {}
            if tty ~= "" and tty ~= "??" and #iterm > 0 then
                local okAS, res = hs.osascript.applescript([[
                    tell application "iTerm2"
                        repeat with w in windows
                            repeat with t in tabs of w
                                repeat with s in sessions of t
                                    if (tty of s) ends with "]] .. tty .. [[" then
                                        select t
                                        select w
                                        activate
                                        return "1"
                                    end if
                                end repeat
                            end repeat
                        end repeat
                    end tell
                    return "0"
                ]])
                if okAS and res == "1" then
                    for _, a in ipairs(iterm) do a:activate(true) end
                    return true
                end
            end
        end
    end
    return false
end

-- NOTE: must be a GLOBAL — a `local` here is garbage-collected after the init
-- chunk finishes, which silently kills the server a few minutes in.
cockpitFocusServer = hs.httpserver.new(false, false)
cockpitFocusServer:setInterface("127.0.0.1")
cockpitFocusServer:setPort(8460)
cockpitFocusServer:setCallback(function(method, path, headers, body)
    local jsonHdr = { ["Content-Type"] = "application/json" }
    local function reply(ok)
        return hs.json.encode({ ok = ok }), 200, jsonHdr
    end
    if method ~= "GET" or not path:match("^/focus") then
        return reply(false)
    end
    -- Pull the candidate needles out of the query string, URL-decoded, and
    -- try them in priority order: title (launcher-stamped --title), alt
    -- (cwd basename), hint (Claude Code's AI task summary — its live
    -- window-retitle text, for windows not launched from the Launcher).
    local query = path:match("%?(.*)$") or ""
    local params = {}
    for k, v in query:gmatch("([^&=?]+)=([^&]*)") do
        params[k] = cockpitUrlDecode(v)
    end
    -- Needle priority. The server may override the default order (e.g. local
    -- sessions send order=hint,title,alt so a fresh ai-title wins over a stale
    -- launcher title); fall back to title,alt,hint when unspecified.
    local seq = {}
    if params.order and params.order ~= "" then
        for k in params.order:gmatch("[^,]+") do seq[#seq + 1] = k end
    else
        seq = { "title", "alt", "hint" }
    end
    local tried, ok = {}, false
    for _, key in ipairs(seq) do
        local needle = params[key]
        if needle and needle ~= "" and not tried[needle] then
            tried[needle] = true
            if cockpitFocusTerminal(needle) then
                ok = true
                break
            end
        end
    end
    -- Last resort for remote sessions: focus the terminal window owning the
    -- interactive `ssh <target>` process.
    if not ok and params.ssh and params.ssh ~= "" then
        ok = cockpitFocusBySsh(params.ssh)
    end
    return reply(ok)
end)
cockpitFocusServer:start()
-- <<< agent-cockpit
