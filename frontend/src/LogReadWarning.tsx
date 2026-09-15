import { ErrorState } from "./ui";

export function LogReadWarning({ log }: { log: "Turns" | "Events" }) {
  return (
    <ErrorState title={`${log} log could not be read`}>
      <span className="log-read-warning__message">
        Available records may be incomplete. Check server log access and refresh after resolving.
      </span>
    </ErrorState>
  );
}
