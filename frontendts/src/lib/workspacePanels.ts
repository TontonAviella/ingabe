export type MobileWorkspacePanel = 'layers' | 'history' | null;

export function toggleMobileWorkspacePanel(
  current: MobileWorkspacePanel,
  requested: Exclude<MobileWorkspacePanel, null>,
): MobileWorkspacePanel {
  return current === requested ? null : requested;
}
