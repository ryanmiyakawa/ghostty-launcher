-- >>> agent-cockpit
-- Agent Cockpit: focus a Ghostty window by title.
-- The dashboard (ghostty_dashboard.py) proxies /api/focus?title=<name>&alt=<fallback>
-- to this local HTTP server. We match a Ghostty window whose title contains the
-- given string (case-insensitive); if nothing matches we retry with `alt`
-- (typically the session's cwd basename — catches windows launched before the
-- Launcher started stamping --title=<project name>). The match raises the window.
--
-- Install: append this whole block (delimiters included) to ~/.hammerspoon/init.lua
-- then reload Hammerspoon (`hs -c "hs.reload()"` or relaunch the app).
-- Requires Hammerspoon's Accessibility permission to focus/raise windows.
require("hs.ipc")
hs.ipc.cliInstall()  -- makes the `hs` CLI work (no-op if already installed)

local function cockpitUrlDecode(s)
    return s:gsub("+", " "):gsub("%%(%x%x)", function(h)
        return string.char(tonumber(h, 16))
    end)
end

local function cockpitFocusGhostty(needle)
    if not needle or needle == "" then return false end
    needle = needle:lower()
    -- Ghostty runs one process per window: hs.application.find() grabs a
    -- single instance (often one with no windows), so iterate EVERY instance
    -- for the bundle id.
    local apps = hs.application.applicationsForBundleID("com.mitchellh.ghostty") or {}
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
    -- Fallback if the bundle lookup came back empty: scan every window on
    -- screen and match by owning-app name.
    if #apps == 0 then
        for _, w in ipairs(hs.window.allWindows()) do
            local app = w:application()
            local an = app and (app:name() or ""):lower() or ""
            local wt = (w:title() or ""):lower()
            if an:find("ghostty", 1, true) and wt ~= "" and wt:find(needle, 1, true) then
                w:focus()
                if app then app:activate(true) end
                return true
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
    -- Pull ?title=...&alt=... out of the query string and URL-decode them.
    local query = path:match("%?(.*)$") or ""
    local title, alt = nil, nil
    for k, v in query:gmatch("([^&=?]+)=([^&]*)") do
        if k == "title" then title = cockpitUrlDecode(v) end
        if k == "alt" then alt = cockpitUrlDecode(v) end
    end
    local ok = cockpitFocusGhostty(title)
    if not ok and alt and alt ~= title then
        ok = cockpitFocusGhostty(alt)
    end
    return reply(ok)
end)
cockpitFocusServer:start()
-- <<< agent-cockpit
