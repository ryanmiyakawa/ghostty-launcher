-- >>> agent-cockpit
-- Agent Cockpit: focus a Ghostty window by title.
-- The dashboard (ghostty_dashboard.py) proxies /api/focus?title=<name> to this
-- local HTTP server. We match a Ghostty window whose title contains the given
-- string (case-insensitive) and raise it. Ghostty windows are titled by the
-- Launcher via `--title=<project name>`, so the project name is the key.
--
-- Install: append this whole block (delimiters included) to ~/.hammerspoon/init.lua
-- then reload Hammerspoon (`hs -c "hs.reload()"` or relaunch the app).
-- Requires Hammerspoon's Accessibility permission to focus/raise windows.
local cockpitFocusServer = hs.httpserver.new(false, false)
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
    -- Pull ?title=... out of the query string and URL-decode it.
    local query = path:match("%?(.*)$") or ""
    local title = nil
    for k, v in query:gmatch("([^&=?]+)=([^&]*)") do
        if k == "title" then title = v end
    end
    if not title or title == "" then return reply(false) end
    title = title:gsub("+", " "):gsub("%%(%x%x)", function(h)
        return string.char(tonumber(h, 16))
    end)
    local needle = title:lower()

    -- Match a Ghostty window whose title contains the requested string.
    local found = false
    local app = hs.application.find("Ghostty")
    if app then
        for _, w in ipairs(app:allWindows()) do
            local wt = (w:title() or ""):lower()
            if wt ~= "" and wt:find(needle, 1, true) then
                w:focus()
                app:activate(true)
                found = true
                break
            end
        end
    end
    return reply(found)
end)
cockpitFocusServer:start()
-- <<< agent-cockpit
