import { Pause } from "lucide-react";

export default function PausedPage() {
  return (
    <main className="paused-page">
      <div className="paused-mark" aria-hidden="true">
        <Pause size={22} strokeWidth={2.5} />
      </div>
      <p className="paused-kicker">IncidentOps</p>
      <h1>Deployment paused</h1>
      <p className="paused-message">
        The IncidentOps demo is temporarily offline while the deployment is being maintained.
      </p>
      <p className="paused-note">Please check back later.</p>
    </main>
  );
}
