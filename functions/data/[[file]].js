/* Cloudflare Pages Function：/data/* 的实时代理。
 *
 * 为什么存在：CF Pages 免费版每月 500 次构建，而本仓库每 ~3 分钟提交一次
 * 数据——若靠构建同步数据，一天就烧光配额。因此 Pages 只构建页面壳子
 * （Build watch paths 排除 data/**），数据请求到边缘后实时回源 GitHub，
 * 30 秒边缘缓存。前端 DATA_BASES 的第一项是相对路径，在 pages.dev 上
 * 自动落到本函数，客户端零改动。
 *
 * 回源顺序：raw.githubusercontent（CF 边缘访问，不经内地网络）→
 * jsDelivr 多端点兜底。只放行白名单文件，不做开放代理。 */

const ALLOW = new Set(["quota.json", "meta.json", "events.json",
                       "history.jsonl", "quota_prev.json"]);
const REPO = "chen1111-a/hkid-quota-monitor";

const ORIGINS = [
  (f) => `https://raw.githubusercontent.com/${REPO}/main/data/${f}`,
  (f) => `https://gcore.jsdelivr.net/gh/${REPO}@main/data/${f}`,
  (f) => `https://cdn.jsdelivr.net/gh/${REPO}@main/data/${f}`,
];

export async function onRequestGet(context) {
  const file = (context.params.file || []).join("/");
  if (!ALLOW.has(file)) {
    return new Response("not found", { status: 404 });
  }
  for (const url of ORIGINS) {
    try {
      const r = await fetch(url(file), {
        cf: { cacheTtl: 30, cacheEverything: true },
      });
      if (r.ok) {
        return new Response(r.body, {
          headers: {
            "content-type": file.endsWith(".jsonl")
              ? "text/plain; charset=utf-8"
              : "application/json; charset=utf-8",
            "cache-control": "public, max-age=15",
            "access-control-allow-origin": "*",
          },
        });
      }
    } catch (e) { /* 换下一个源 */ }
  }
  return new Response("upstream unavailable", { status: 502 });
}
