/* 调度自证的代理：api.github.com 在内地移动网络大概率不通，看板的
 * 绿勾审计条因此常年显示「网络不可达」。pages.dev 可达时走本函数
 * 从边缘回源 GitHub API，60 秒缓存（审计条本来就是分钟级信息）。 */

const REPO = "chen1111-a/hkid-quota-monitor";

export async function onRequestGet() {
  try {
    const r = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/monitor.yml/runs?per_page=12&status=completed`,
      {
        headers: {
          // GitHub API 无 UA 直接 403
          "user-agent": "hkid-quota-monitor-pages-proxy",
          "accept": "application/vnd.github+json",
        },
        cf: { cacheTtl: 60, cacheEverything: true },
      });
    if (!r.ok) return new Response("upstream " + r.status, { status: 502 });
    return new Response(r.body, {
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "public, max-age=30",
        "access-control-allow-origin": "*",
      },
    });
  } catch (e) {
    return new Response("upstream unavailable", { status: 502 });
  }
}
