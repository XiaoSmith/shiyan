# Twitter 标签插件 · 数据层（xbangdan）

## 已拍板，不再改的几条

- 抓取目标只认 xbangdan.com，不抓 x.com，GitHub 抓取项目只当参考。
- 不搞人工种子名单。抓取挂了靠更稳的抓取 + 保留旧数据 + 插件缓存降级。
- 关系类标签见 `relationship-tags.md`，已定稿 6 个：已拉黑、已静音、互关、粉丝、已关注，外加可叠加的常互动。

## 这套东西怎么跑

```
GitHub Actions（每天 2 次）
  -> scripts/fetch_full.py 用无头浏览器翻页抓完整榜单
  -> 生成 tags.json（失败不覆盖旧文件）
  -> 插件从 GitHub Pages / jsDelivr 拉 tags.json，按 @handle 匹配打标签
```

插件端建议的降级顺序：当日榜单 > 昨日缓存 > 不显示。

## 标签怎么定的（数据标签只剩 1 个，2026-10-27 定稿）

- 黑马：黑马筛选页（5000 粉以下新锐号）翻页后的全部号，网站已经判定过，不再自己加粉丝/帖均门槛。
- 已删除：今日爆款、今日涨粉、长文作者、全部赛道标签（AI、加密、搞钱、科技、时事）。
- 关系类标签另见 `relationship-tags.md`，已定稿 6 个，不受影响。
- 账号属性类标签正在设计中，见下节，尚未定稿。

## 部署步骤

1. 新建一个公开仓库，把本目录所有文件（含 `.github/`）推上去。
2. Actions 会按 `.github/workflows/update-tags.yml` 每天自动跑 2 次，
   也可以在 Actions 页面点 Run workflow 手动跑一次验证。
3. 插件拉取地址二选一：
   - GitHub Pages：开启 Pages 后用 `https://<用户名>.github.io/<仓库>/tags.json`
   - jsDelivr：`https://cdn.jsdelivr.net/gh/<用户名>/<仓库>@main/tags.json`
4. 插件本地缓存 tags.json 24 小时，拉取失败就用缓存，不要每次打开推特都重新拉。

## 第一次真实运行要调什么

脚本是按“区块标题关键词 + x.com 链接 + 头像文件名”三路交叉解析的，
不依赖具体 class 名。但 xbangdan 的真实 HTML 结构只有第一次跑才能完全确认，
如果解析到的号远低于阈值（默认 20 个），脚本会主动失败、不覆盖旧数据，
这时先看 Actions 日志里的 `[parse]` 行，再调 `SECTION_TAGS` 和解析逻辑，
不要把阈值调低来蒙混过关。

## 成本

公开仓库 + GitHub Actions + GitHub Pages / jsDelivr，全程零服务器、零数据库、
零 X API Key。每天 2 次、每次 1-2 分钟，远在免费额度内。
