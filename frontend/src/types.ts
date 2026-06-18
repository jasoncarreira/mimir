export type Bootstrap = {
  auth: {
    required: boolean;
    scheme: string;
    storage: string;
  };
  server: {
    web_host: string;
    public_bind: boolean;
    unauthenticated_allowed: boolean;
  };
  stream_auth: {
    shape: string;
    header: string;
    native_eventsource_supported_when_auth_required: boolean;
  };
};

export type AppSearch = {
  turn?: string;
  tab?: string;
  filter?: string;
  detail?: string;
};

export type ShellRoute = {
  id: string;
  path: string;
  label: string;
  summary: string;
  legacyHref?: string;
};
