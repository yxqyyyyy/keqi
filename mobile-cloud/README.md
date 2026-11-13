# 课期手机安排

这个可选服务提供适合 iPhone、iPad 使用的作业与考试页面，可新增安排、修改时间与备注、勾选完成和删除。手机通过 HTTPS 访问云端，Mac 恢复运行并联网后接收修改，再更新本机程序和已启用的日历同步。

学习通、希冀的登录会话保存在 Mac。云端保存任务标题、课程、时间、状态、链接、备注及修改记录。Mac 关闭期间，手机修改会进入云端队列。已有日历提醒可能继续触发，直到 Mac 接收修改并完成日历同步。考试提醒仍以开始时间计算。

## 访问与身份验证

部署需要启用私人 Sites 的 Sign in with ChatGPT（SIWC）访问控制。Sites 网关验证访问者身份，并注入 `oai-authenticated-user-id` 等身份头。浏览器接口依赖这个可信网关，首次访问会把当前数据集绑定到本人。

手机网络需要能够访问托管域名及 ChatGPT 登录服务。部分网络可能遇到网关拦截或连接超时。页面请求超过 15 秒会显示网络提示，Mac 后台以明确的应用标识请求，并在失败后保留本机数据和自动重试。普通网络下的可达性仍取决于托管服务及登录入口。

直接把 Worker 部署到可公开访问的地址并信任来访者提交的同名请求头，会允许身份伪造。迁移到其他托管服务时，必须接入该服务的服务端身份验证、访问控制，并阻止外部伪造身份头，再开放浏览器接口。可通过 `KEQI_OWNER_ID` 进一步限定站点内的用户标识。

Mac 桥接接口还使用独立的 `KEQI_BRIDGE_KEY`。请在服务端环境中配置至少 32 字符的随机连接密钥，并在 Mac 使用相同值。私人 Sites 的机器访问还需要有效的 Sites 访问凭证。凭证和实际部署配置应保存在本地或托管服务的私密配置中。

## 准备自己的实例

通过 Sites 为自己创建私人项目并配置 D1，绑定名使用 `DB`。将通用示例复制到本地配置位置，再由自己的项目配置部署信息。

```sh
mkdir -p .openai
cp hosting.example.json .openai/hosting.json
```

`hosting.example.json` 仅声明 `d1` 和 `r2`。请将实际 `.openai/hosting.json` 加入本地忽略规则，保留示例文件供他人配置。此源码包排除了实际部署标识、环境变量文件、运行目录和依赖目录。

Node.js 版本要求见 `package.json`。安装、构建及本地 D1 初始化可使用以下命令。

```sh
npm run install:ci
npm run build
node --import ./scripts/sites-env.mjs ./node_modules/wrangler/bin/wrangler.js d1 execute DB --local --config dist/server/wrangler.json --persist-to .wrangler/state --file drizzle/0000_short_chameleon.sql
npm run dev
```

迁移应在每个环境中按顺序应用一次。部署环境需要配置私人访问、D1、服务端连接密钥并应用 `drizzle/` 下的数据库迁移。本地便携预览支持 SIWC 模拟登录，用于开发验证。生产访问控制由 Sites 网关负责。

模型与同步测试需要支持 TypeScript 类型擦除、`node:sqlite` 和模块加载钩子的 Node.js 版本。

```sh
node --experimental-strip-types --test tests/*.test.mjs
```

## 连接 Mac

Mac 本机的 `PUT /api/cloud/config` 接受以下连接参数。请求需要发送到本机服务，例如 `http://127.0.0.1:8765/api/cloud/config`，该配置接口只允许从 Mac 本机访问。

```json
{
  "url": "https://your-private-site.example",
  "bridge_key": "YOUR_RANDOM_BRIDGE_KEY_AT_LEAST_32_CHARACTERS",
  "sites_token": "YOUR_PRIVATE_SITES_TOKEN"
}
```

`url` 填自己的 HTTPS 站点首页。`bridge_key` 与服务端 `KEQI_BRIDGE_KEY` 一致。私人 Sites 需要 `sites_token`，接入其他已完成身份验证的托管服务时可留空。Mac 将这些参数保存在本地私有数据目录。

手机修改具有唯一操作编号。Mac 保存修改及回执后，重复传送同一操作会返回原回执。不同字段的修改可以合并，同一字段出现冲突会显示待处理状态，需查看最新内容后重新修改或放弃该条修改。人工修改的字段会在后续平台读取时保留，已确认提交的任务会停止提醒。

## 源码与许可

`app/` 是手机页面和接口，`lib/keqi-model.ts`、`lib/keqi-sync.ts` 定义任务、修改队列与同步规则，`db/`、`drizzle/` 提供 D1 数据结构和迁移。`worker/`、`scripts/` 来自运行模板，`vendor/` 保留随模板提供的资源及许可证。第三方说明见项目根目录 `THIRD_PARTY_NOTICES.md`。
