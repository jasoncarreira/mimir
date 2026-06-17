(function () {
  "use strict";

  var API_KEY_LS = "mimir.api_key";

  function getApiKey() {
    try {
      return window.localStorage.getItem(API_KEY_LS) || "";
    } catch (e) {
      return "";
    }
  }

  function setApiKey(key) {
    try {
      if (key) window.localStorage.setItem(API_KEY_LS, key);
      else window.localStorage.removeItem(API_KEY_LS);
    } catch (e) {
      // Private browsing or blocked storage. Calls continue unauthenticated.
    }
  }

  function promptApiKey(reason) {
    var msg = "Enter MIMIR_API_KEY";
    if (reason) msg += " (" + reason + ")";
    msg += ":\n\n(Saved to this browser; leave blank to skip.)";
    var value = (window.prompt(msg, "") || "").trim();
    setApiKey(value);
    return value;
  }

  function authHeaders(extra) {
    var headers = Object.assign({}, extra || {});
    var key = getApiKey();
    if (key) headers["X-API-Key"] = key;
    return headers;
  }

  function authedFetch(url, opts) {
    opts = opts || {};
    opts.headers = authHeaders(opts.headers);
    return window.fetch(url, opts).then(function (response) {
      if (response.status === 401) {
        setApiKey("");
        var fresh = promptApiKey("previous key was rejected");
        if (fresh) {
          opts.headers = authHeaders(opts.headers);
          return window.fetch(url, opts);
        }
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
