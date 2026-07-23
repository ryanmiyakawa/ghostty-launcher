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
    local tried, ok = {}, false
    for _, key in ipairs({ "title", "alt", "hint" }) do
        local needle = params[key]
        if needle and needle ~= "" and not tried[needle] then
            tried[needle] = true
            if cockpitFocusTerminal(needle) then
                ok = true
                break
            end
        end
    end
    return reply(ok)
end)
cockpitFocusServer:start()
-- <<< agent-cockpit
