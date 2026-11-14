/** 毫秒 → 「X 秒」/「X 分 Y 秒」。
 *
 *  对话页的摘要行（ChatView）与阶段时间线（StepTimeline）共用一份，不各留副本。
 */
export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000))
  if (seconds < 60) return `${seconds} 秒`
  return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`
}
