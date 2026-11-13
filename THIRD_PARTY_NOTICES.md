# 第三方资料与许可证

`studyreminder/chaoxing_adapter.py` 的只读页面入口和字段结构参考了
[DanMo661/Xuexitong-mcp](https://github.com/DanMo661/Xuexitong-mcp)，
具体参考版本为提交 `ebce2cc735bc7acc45fac7ccf32bcefe1a6b2a54`（2026-09-15），
文件为 `xuexitong_mcp/client.py`。适配器自行实现 Playwright 会话访问、HTML
解析、严格完整日期检查和身份校验，未引入该项目的账号密码登录、自动重登及
其他非作业/考试功能。公开项目的协议说明并非超星官方接口保证。

该项目使用 MIT License，原版权声明及许可如下：

MIT License

Copyright (c) 2026 Wu Jiale

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

研究中亦检查了 `Cooanyh/chaoxing-list`（AGPL-3.0-or-later）以及
`coursegrading/CourseGrading`（公开资料仓库，未发现许可证）；当前程序没有复制
或包含这两个仓库的代码。

## 手机云端模板

`mobile-cloud/` 使用 Sites 提供的 Vinext starter，保留其 Worker 入口、连接器预览和构建辅助文件。
随模板提供的 Sites Vite 插件使用 MIT License，Copyright (c) 2026 OpenAI，
完整许可保留在 `mobile-cloud/scripts/vite/sites-vite-plugin.LICENSE`。

随模板提供的 shadcn Tailwind 样式资源使用 MIT License，Copyright (c) 2023 shadcn，
完整许可保留在 `mobile-cloud/vendor/shadcn-tailwind-4.13.0.LICENSE.md`。

手机云端的依赖和锁定版本见 `mobile-cloud/package.json` 与 `mobile-cloud/package-lock.json`。
各依赖的许可证随对应软件分发，重新分发时应保留其适用的许可声明。
