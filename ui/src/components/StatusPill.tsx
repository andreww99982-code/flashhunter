/** Цветной статус-чип (M3 «assist chip»). */
const map: Record<string, string> = {
  success: 'pill-profit',
  filled: 'pill-profit',
  ok: 'pill-profit',
  running: 'pill-profit',
  connected: 'pill-profit',
  verified: 'pill-profit',
  submitted: 'pill-info',
  simulated: 'pill-info',
  detected: 'pill-info',
  pending: 'pill-warn',
  degraded: 'pill-warn',
  rejected: 'pill-warn',
  dropped: 'pill-loss',
  reverted: 'pill-loss',
  failed: 'pill-loss',
  error: 'pill-loss',
  cancelled: 'pill',
  undeployed: 'pill',
  disabled: 'pill',
  expired: 'pill',
  dry_run: 'pill-warn',
};

export function StatusPill({ status, label }: { status: string; label?: string }) {
  const cls = map[status] ?? 'pill';
  return <span className={`pill ${cls} num`}>{label ?? status}</span>;
}
