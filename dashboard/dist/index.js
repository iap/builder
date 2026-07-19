"use strict";

/**
 * AWS Build (Amazon Builder ID) auth dashboard card.
 *
 * Uses the Hermes plugin SDK exposed on window:
 *   - window.__HERMES_PLUGINS__.register(name, Component)  (required!)
 *   - window.__HERMES_PLUGIN_SDK__  (React, hooks, api, authedFetch, components)
 *
 * The host injects this bundle as an <script async>, then checks whether we
 * called register() within a microtask. So we MUST call register() at the top
 * level (no awaits before it).
 *
 * Flow: a single "Login with Build ID" button starts an RFC 8628 device flow.
 * The backend returns a verification URL + user_code; we open the URL in a new
 * tab and show the code. The card then polls GET /status (which actively polls
 * the in-flight flow) until the human approves in their browser. No headless
 * browser is launched here — the human does the approval where the code is
 * shown.
 */

(function () {
  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS) {
    console.error("[aws-build] Hermes plugin SDK not available");
    return;
  }

  const React = SDK.React;
  const { useState, useEffect, useRef, useCallback } = SDK.hooks;
  const { authedFetch } = SDK;
  const C = SDK.components;

  const API_BASE = "/api/plugins/aws-build";

  function authHeaders() {
    return { "Content-Type": "application/json" };
  }

  async function apiFetch(path, opts) {
    const res = await authedFetch(`${API_BASE}${path}`, {
      ...opts,
      headers: authHeaders(),
    });
    return res.json();
  }

  function Icon({ name }) {
    const paths = {
      KeyRound:
        "M16 10a4 4 0 1 0-8 0v2H5v8h14v-8h-3v-2zm-6 0a2 2 0 1 1 4 0v2h-4v-2z",
      LogIn:
        "M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3",
      LogOut:
        "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
      Refresh:
        "M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5",
      Check: "M20 6 9 17l-5-5",
      Alert:
        "M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
      Copy: "M9 9h11a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2zM5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1",
    };
    const d = paths[name] || paths.KeyRound;
    return React.createElement(
      "svg",
      {
        width: 16,
        height: 16,
        viewBox: "0 0 24 24",
        fill: "none",
        stroke: "currentColor",
        strokeWidth: 2,
        strokeLinecap: "round",
        strokeLinejoin: "round",
        "aria-hidden": "true",
      },
      React.createElement("path", { d: d }),
    );
  }

  function App() {
    const [status, setStatus] = useState({ loading: true });
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState(null);
    const [login, setLogin] = useState(null); // { user_code, verification_uri_complete }
    const pollTimer = useRef(null);

    const stopPolling = useCallback(() => {
      if (pollTimer.current) {
        clearInterval(pollTimer.current);
        pollTimer.current = null;
      }
    }, []);

    const refresh = useCallback(async () => {
      try {
        const data = await apiFetch("/status");
        setStatus({ loading: false, ...data });
        // Drive polling while a flow is pending and unauthenticated.
        if (data.authenticated) {
          stopPolling();
          setLogin(null);
        } else if (data.phase === "awaiting_approval") {
          if (!pollTimer.current) {
            pollTimer.current = setInterval(refresh, 2500);
          }
        } else {
          stopPolling();
        }
        return data;
      } catch (e) {
        setStatus({ loading: false, authenticated: false, error: String(e) });
        stopPolling();
        return null;
      }
    }, [stopPolling]);

    useEffect(() => {
      refresh();
      return stopPolling;
    }, [refresh, stopPolling]);

    const doLogin = async () => {
      setBusy(true);
      setMsg(null);
      try {
        const data = await apiFetch("/login", { method: "POST", body: "{}" });
        if (data.success) {
          setLogin({
            user_code: data.user_code,
            verification_uri_complete: data.verification_uri_complete,
          });
          if (data.verification_uri_complete) {
            window.open(
              data.verification_uri_complete,
              "_blank",
              "noopener,noreferrer",
            );
          }
          setMsg({
            type: "ok",
            text:
              "Verification opened in a new tab. Enter the code below and approve in your browser — this card polls automatically.",
          });
          // Begin polling for completion.
          stopPolling();
          pollTimer.current = setInterval(refresh, 2500);
        } else {
          setMsg({ type: "err", text: data.error || "Login failed" });
        }
      } catch (e) {
        setMsg({ type: "err", text: String(e) });
      } finally {
        setBusy(false);
      }
    };

    const copyCode = async () => {
      if (!login || !login.user_code) return;
      try {
        await navigator.clipboard.writeText(login.user_code);
        setMsg({ type: "ok", text: "User code copied to clipboard." });
      } catch (e) {
        setMsg({ type: "err", text: String(e) });
      }
    };

    const doLogout = async () => {
      setBusy(true);
      setMsg(null);
      stopPolling();
      try {
        await apiFetch("/logout", { method: "POST" });
        setLogin(null);
        setMsg({ type: "ok", text: "Logged out" });
      } catch (e) {
        setMsg({ type: "err", text: String(e) });
      } finally {
        setBusy(false);
        await refresh();
      }
    };

    const authenticated = status.authenticated && !status.error;
    const awaiting = status.phase === "awaiting_approval" && !authenticated;

    return React.createElement(
      "div",
      { className: "ab-wrap" },
      React.createElement(
        C.Card,
        null,
        React.createElement(
          "div",
          { className: "ab-card-header" },
          React.createElement(Icon, { name: "KeyRound" }),
          React.createElement("span", null, "AWS Build"),
          React.createElement(
            "span",
            { className: "ab-sub" },
            "Amazon Builder ID",
          ),
        ),
        React.createElement(
          "div",
          { className: "ab-card-body" },
          React.createElement(
            "div",
            { className: "ab-status-row" },
            status.loading
              ? React.createElement(
                  "span",
                  { className: "ab-muted" },
                  "Checking status…",
                )
              : authenticated
                ? React.createElement(
                    C.Badge,
                    { variant: "success" },
                    React.createElement(Icon, { name: "Check" }),
                    " Authenticated",
                  )
                : awaiting
                  ? React.createElement(
                      C.Badge,
                      { variant: "warning" },
                      " Awaiting approval",
                    )
                  : React.createElement(
                      C.Badge,
                      { variant: "destructive" },
                      React.createElement(Icon, { name: "Alert" }),
                      " Not logged in",
                    ),
            React.createElement(
              C.Button,
              {
                variant: "ghost",
                size: "icon",
                onClick: refresh,
                disabled: busy,
                "aria-label": "Refresh",
              },
              React.createElement(Icon, { name: "Refresh" }),
            ),
          ),
          authenticated && status.token_expires_at_iso
            ? React.createElement(
                "div",
                { className: "ab-meta" },
                "Expires: " + status.token_expires_at_iso,
              )
            : null,
          status.error
            ? React.createElement(
                "div",
                { className: "ab-meta ab-err" },
                "Error: " + status.error,
              )
            : null,
          status.phase === "idle" && !authenticated
            ? React.createElement(
                "div",
                { className: "ab-meta ab-muted" },
                "No active device flow.",
              )
            : null,
          msg
            ? React.createElement(
                "div",
                { className: "ab-msg " + (msg.type === "ok" ? "ok" : "err") },
                msg.text,
              )
            : null,

          // Device-flow code display (shown while awaiting approval).
          awaiting && login && login.user_code
            ? React.createElement(
                "div",
                { className: "ab-code-box" },
                React.createElement(
                  "div",
                  { className: "ab-label" },
                  "Enter this code in the browser tab:",
                ),
                React.createElement(
                  "div",
                  { className: "ab-code-row" },
                  React.createElement(
                    "code",
                    { className: "ab-code" },
                    login.user_code,
                  ),
                  React.createElement(
                    C.Button,
                    {
                      variant: "outline",
                      size: "sm",
                      onClick: copyCode,
                      disabled: busy,
                    },
                    React.createElement(Icon, { name: "Copy" }),
                    " Copy",
                  ),
                ),
              )
            : null,

          // Login / Logout control.
          !authenticated
            ? React.createElement(
                "div",
                { className: "ab-login-section" },
                React.createElement(
                  C.Button,
                  {
                    variant: "default",
                    size: "lg",
                    onClick: doLogin,
                    disabled: busy,
                    className: "w-full",
                  },
                  React.createElement(Icon, { name: "LogIn" }),
                  " Login with Build ID",
                ),
                React.createElement(
                  "p",
                  { className: "ab-hint" },
                  "Starts a device login (RFC 8628). The verification page opens in a new tab; approve there and this card updates automatically. Uses the plugin's own Amazon Builder ID flow — no Hermes credential pool involved.",
                ),
              )
            : React.createElement(
                C.Button,
                {
                  variant: "outline",
                  size: "sm",
                  onClick: doLogout,
                  disabled: busy,
                },
                React.createElement(Icon, { name: "LogOut" }),
                " Logout",
              ),
        ),
      ),
    );
  }

  // REQUIRED: register the tab component with the host.
  PLUGINS.register("aws-build", App);
})();
