# CloudMail 部署指南：在 Cloudflare 上搭建属于自己的域名邮箱

## 项目简介

CloudMail 是一个基于 Cloudflare 生态的简约响应式邮箱服务项目。它的核心思路很直接：你只需要拥有一个域名，并将其托管在 Cloudflare，就可以借助 Workers、D1、KV、R2 等免费资源，搭建一套功能完整的个人或团队邮箱系统。

它并非传统意义上跑在 VPS 上的 Postfix/Dovecot 套件，而是将收件交给 Cloudflare Email Routing，发件通过 Resend 或 Cloudflare Email 服务完成，核心业务逻辑则运行在 Cloudflare Workers 上。这意味着你不需要维护服务器、不用担心证书续期，免费额度对于个人日常使用来说通常绰绰有余。

功能上，CloudMail 支持多用户管理、附件收发（依赖 R2）、邮件推送至 Telegram 机器人、开放 API、验证码自动识别以及数据可视化等。技术栈方面，后端基于 Hono 框架，前端是 Vue3 + Element Plus，数据库用 D1，缓存用 KV，附件存 R2。


## 需要材料

在开始之前，请确认你手头有以下几样东西：

**一个托管在 Cloudflare 的域名**。这是硬性前提。域名的 NS 记录需要指向 Cloudflare，状态显示为 Active。如果还没有域名，可以先去注册一个便宜的，或者使用免费二级域名（但免费域名在后续 Resend 验证等环节可能受限）。

**一个 Cloudflare 账号**。免费版即可，但需要注意 R2 对象存储虽然每月有 10GB 免费额度，开通时可能要求绑定支付方式。

**一个 Resend 账号（用于发件）**。Cloudflare 自身不直接提供外发邮件的 SMTP 服务，CloudMail 发信依赖 Resend 的 API。你需要注册 Resend，添加自己的发件域名，并完成 DNS 验证（SPF、DKIM 等记录）。

**本地环境或 GitHub 账号**。你可以选择本地用 Wrangler CLI 部署，也可以 Fork 仓库后通过 Cloudflare Dashboard 连接 GitHub 自动构建。后者对新手更友好，省去本地 Node.js 环境配置的麻烦。


## 具体过程

### 第一步：创建 Cloudflare 资源

登录 Cloudflare Dashboard，提前创建好三个资源，记下它们的 ID 或名称：

**D1 数据库**：进入“存储和数据库 → D1”，创建一个数据库，名称随意（比如 `cloud-mail-db`）。创建后复制 **Database ID**，后面绑定要用。

**KV 命名空间**：进入“存储和数据库 → KV”，创建一个命名空间（比如 `cloud-mail-kv`），复制 **Namespace ID**。

**R2 存储桶（可选）**：如果需要收发附件，创建一个 R2 桶。绑定变量名统一用 `r2`。


### 第二步：部署 Worker

**方式 A：GitHub 自动部署（推荐新手）**

Fork `maillab/cloud-mail` 仓库到你的 GitHub。在 Cloudflare Dashboard 进入 Workers & Pages，点击“创建”，选择“连接到 Git”，授权并选中你 fork 的仓库。**关键一步**：构建路径必须填 `/mail-worker`，否则会找不到项目。部署时会提示添加环境变量，先填入 `admin`（你的管理员邮箱，如 `admin@yourdomain.com`）、`domain`（JSON 格式的域名数组，如 `["yourdomain.com"]`）、`jwt_secret`（一串随机长字符串，记好，初始化要用）。点击部署，等待构建完成。

**方式 B：本地 Wrangler 部署**

```bash
git clone https://github.com/maillab/cloud-mail.git
cd cloud-mail/mail-worker
pnpm install
npx wrangler login
npx wrangler deploy
```

注意：仓库的根目录构建脚本可能只安装前端依赖，`mail-worker` 的依赖需要手动 `pnpm install`，否则 `wrangler deploy` 会报找不到 `drizzle-orm` 等模块。部署前需要在 `wrangler.toml` 中填好 D1 和 KV 的 ID，或者部署后在 Dashboard 里补绑定。


### 第三步：绑定资源与自定义域名

Worker 部署成功后，进入该 Worker 的设置页面：

**绑定 D1 和 KV**：在“设置 → 绑定”中，添加 D1 数据库绑定，变量名必须填 `db`；添加 KV 命名空间绑定，变量名必须填 `kv`。变量名写错程序会找不到资源。

**绑定自定义域名**：在“设置 → 域和路由”中，添加一个自定义域名，比如 `mail.yourdomain.com`。Cloudflare 会自动创建 DNS 记录。不建议长期使用 `*.workers.dev` 默认域名，因为它在部分网络环境下解析不稳定。


### 第四步：初始化数据库

在浏览器中访问：

```
https://mail.yourdomain.com/api/init/你的jwt_secret
```

如果返回 `success`，说明 D1 中的表结构已成功创建。如果返回 `Database not initialized` 或类似错误，检查 `jwt_secret` 是否与 Worker 环境变量中设置的一致。


### 第五步：配置收件路由

进入 Cloudflare Dashboard 的域名管理页面，找到“电子邮件 → Email Routing”。启用 Email Routing，然后在“路由规则”中添加一条 **Catch-all（全收）**规则，操作选择发送到 Worker，并选中你刚部署的 `cloud-mail` Worker。这样所有发往 `*@yourdomain.com` 的邮件都会进入 CloudMail 处理。


### 第六步：注册管理员并配置发件

访问 `https://mail.yourdomain.com`，用你设置的 `admin` 邮箱注册第一个账号，该账号会自动成为管理员。**注册完成后立刻去系统设置里关闭“开放注册”**，防止陌生人注册。

发件方面，如果你使用 Resend：先在 Resend 后台添加你的域名，按提示在 Cloudflare DNS 中添加 SPF、DKIM 等记录，等待验证通过。然后在 CloudMail 后台的系统设置中填入 Resend 的 API Key。发件开关的配置需要留意：后台界面显示“开启”时，数据库 `setting` 表中对应的值可能是 `0` 而非 `1`，如果发信报 403，检查一下这个反直觉的设定。


## 可能遇到的报错

**`Hostname already has externally managed DNS records`（code: 100117）**

绑定自定义域名时出现。原因是你想用的子域名（如 `mail.yourdomain.com`）在 Cloudflare DNS 中已经存在 A 或 CNAME 记录，或者被其他 Worker/Pages 项目占用了。去 DNS 记录页面删除冲突项，或换一个子域名即可。

**`KV namespace is not valid`（code: 10042）**

在 GitHub Actions 或环境变量中填 KV/D1 的 ID 时，误填成了资源名称（如 `cloud-mail-kv`）而不是真正的 ID。D1 的 ID 在数据库详情页复制，KV 的 ID 在命名空间详情页复制，是一长串 UUID 格式的字符。如果嫌麻烦，也可以干脆不填 ID 只填名称，让脚本自动查找同名资源。

**初始化接口返回 403 或被 Cloudflare 拦截**

如果你通过 GitHub Actions 自动部署，最后一步初始化可能因为 GitHub 服务器 IP 被 Cloudflare 的机器人防护拦截而失败。页面返回的是 `Just a moment...` 的 JS 挑战页。解决办法：进入 Cloudflare 安全性设置，**关闭“Bot Fight 模式”**。注意，WAF 的自定义跳过规则无法覆盖 Bot Fight 模式，必须单独关。

**Worker 构建失败提示 `root directory not found`**

在 Cloudflare Dashboard 连接 GitHub 部署时，构建路径没有设置为 `/mail-worker`。修改构建配置，把根目录指向 `mail-worker` 子目录。

**发信返回 403 “邮件发送功能已停用”**

即使后台界面里开关显示为“开启”，数据库 `setting` 表中的实际语义可能是反的（0 为开启，1 为关闭）。直接去 D1 控制台执行 SQL 查看 `setting` 表，把发件相关开关设为 `0` 试试。

**收不到邮件，D1 里也没有记录**

检查 `noRecipient` 变量的设置。如果它默认为 `1`，而发件人使用的收件地址尚未在 CloudMail 系统中创建，Cloudflare 会直接拒收，邮件不会入库。要么先在后台创建对应邮箱账号，要么调整该配置。另外确认 Email Routing 的 Catch-all 规则状态是否为“活跃”。


## 结语

CloudMail 的部署过程不算复杂，但踩坑点主要集中在“变量名必须精确匹配”、“开关语义反直觉”以及“Cloudflare 安全防护拦截自动化请求”这几类问题上。真正花时间的往往不是写代码或点按钮，而是排查这些配置层面的偏差。

一旦跑通，你得到的是一个零服务器成本、带 Web 界面、支持多用户和附件的域名邮箱系统。对于个人折腾、小团队内部使用，或者单纯想摆脱第三方邮箱平台对自定义域名的收费限制，这套方案都相当实用。建议部署前先把项目仓库的 `wrangler.toml` 和 D1 初始化脚本大致翻一遍，对整体结构有个印象，能帮你少绕不少弯路。
