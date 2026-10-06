(function () {
  "use strict";

  var API_KEY_LS = "mimir.api_key";

  // Exchange a legacy key once, and remove it even if the exchange fails.
  var oldKey = "";
  try {
    oldKey = window.localStorage.getItem(API_KEY_LS) || "";
    window.localStorage.removeItem(API_KEY_LS);
  } catch (e) { /* storage may be disabled */ }
  var sessionReady = oldKey
    ? window.fetch("/api/v1/web/session", {
        method: "POST", headers: {"X-API-Key": oldKey}
      }).catch(function () { /* a rejected legacy key has no session */ })
    : Promise.resolve();
  oldKey = "";

  function getApiKey() {
    // The HttpOnly credential cannot be read from JS. Legacy callers only use
    // truthiness to decide whether to prompt on first visit; fetch handles 401.
    return "session-cookie";
  }

  function setApiKey(key) {
    sessionReady = sessionReady.catch(function () {}).then(function () {
      return window.fetch("/api/v1/web/session", key
        ? {method: "POST", headers: {"X-API-Key": key}}
        : {method: "DELETE"}).then(function (response) {
          if (!response.ok) throw new Error("Invalid API key");
        });
    });
    return sessionReady;
  }

  function promptApiKey(reason) {
    var msg = "Enter MIMIR_API_KEY";
    if (reason) msg += " (" + reason + ")";
    msg += ":\n\n(Used for an HttpOnly session cookie; leave blank to skip.)";
    var value = (window.prompt(msg, "") || "").trim();
    if (!value) return Promise.resolve("");
    return setApiKey(value).then(function () { return value; });
  }

  function authHeaders(extra) {
    return Object.assign({}, extra || {});
  }

  function authedFetch(url, opts) {
    opts = opts || {};
    opts.headers = authHeaders(opts.headers);
    return sessionReady.catch(function () {}).then(function () {
      return window.fetch(url, opts);
    }).then(function (response) {
      if (response.status === 401) {
        return promptApiKey("previous key was rejected").then(function (fresh) {
          return fresh ? window.fetch(url, opts) : response;
        });
      }
      return response;
    });
  }

  function authedJson(url, opts) {
    return authedFetch(url, opts).then(function (response) {
      if (response.status === 401) {
        throw new Error("Unauthorized - bad API key?");
      }
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      return response.json();
    });
  }

  function reset(reason) {
    setApiKey("");
    return promptApiKey(reason || "manual rotation");
  }

  function wireResetLink(id, onReset) {
    window.addEventListener("DOMContentLoaded", function () {
      var link = document.getElementById(id || "reset-key-link");
      if (!link) return;
      link.style.display = getApiKey() ? "" : "none";
      if (!link.getAttribute("onclick")) {
        link.addEventListener("click", function (event) {
          event.preventDefault();
          reset("manual rotation");
          if (onReset) onReset();
        });
      }
    });
  }

  function fetchEventStream(url, handlers) {
    handlers = handlers || {};
    var controller = new AbortController();
    authedFetch(url, {
      headers: {"Accept": "text/event-stream"},
      signal: controller.signal,
    }).then(function (response) {
      if (!response.ok || !response.body) {
        if (handlers.onerror) handlers.onerror(response);
        return;
      }
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";

      function pump() {
        reader.read().then(function (chunk) {
          if (chunk.done) return;
          buffer += decoder.decode(chunk.value, {stream: true});
          var parts = buffer.split("\n\n");
          buffer = parts.pop() || "";
          parts.forEach(function (part) {
            part.split("\n").forEach(function (line) {
              if (line.indexOf("data: ") === 0 && handlers.onmessage) {
                handlers.onmessage({data: line.slice(6)});
              }
            });
          });
          pump();
        }).catch(function (error) {
          if (handlers.onerror) handlers.onerror(error);
        });
      }

      pump();
    }).catch(function (error) {
      if (handlers.onerror) handlers.onerror(error);
    });
    return controller;
  }

  window.MimirAuth = {
    storageKey: API_KEY_LS,
    getApiKey: getApiKey,
    setApiKey: setApiKey,
    promptApiKey: promptApiKey,
    authHeaders: authHeaders,
    authedFetch: authedFetch,
    authedJson: authedJson,
    reset: reset,
    wireResetLink: wireResetLink,
    fetchEventStream: fetchEventStream,
  };
}());
