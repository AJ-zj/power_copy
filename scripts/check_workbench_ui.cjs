const assert = require('node:assert/strict')
const path = require('node:path')
const { mkdir } = require('node:fs/promises')
const { chromium } = require(process.env.POWERCOPY_PLAYWRIGHT || 'playwright')

const baseUrl = process.env.POWERCOPY_WEB_URL || 'http://127.0.0.1:5173'
const screenshots = path.resolve(__dirname, '../docs/screenshots')

async function check() {
  await mkdir(screenshots, { recursive: true })
  const browser = await chromium.launch({
    executablePath: process.env.POWERCOPY_BROWSER || undefined,
    headless: true,
  })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1024 } })
    page.setDefaultTimeout(10000)
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    page.on('response', response => {
      if (response.url().includes('/api/workbench/') && response.status() >= 400) {
        console.warn('HTTP', response.status(), new URL(response.url()).pathname)
      }
    })

    async function visit(view, table, title, params = '') {
      await page.goto(baseUrl + '/#/data/' + view + params)
      await page.getByRole('heading', { name: title, exact: true }).waitFor()
      const records = page.getByRole('table', { name: table, exact: true })
      try { await records.waitFor() } catch (error) {
        console.error('Page state:', await page.locator('main').innerText(), errors)
        throw error
      }
      return records
    }

    const initial = await visit('policies', '政策列表', '政策库')
    const policyNames = initial.locator('tbody .dc-policy-title')
    const title = (await policyNames.first().innerText()).trim()
    await page.getByRole('textbox', { name: '搜索标题或正文' }).fill(title.slice(0, 4))
    await page.getByRole('button', { name: '搜索', exact: true }).click()
    const match = initial.getByRole('button', { name: title, exact: true })
    await match.waitFor()
    await match.click()
    await page.getByRole('heading', { name: '原始正文' }).waitFor()
    assert.ok((await page.locator('.dc-body pre').innerText()).trim().length > 25)
    assert.match(await page.getByRole('link', { name: '打开官方来源' }).getAttribute('href'), /^https:\/\//)
    await page.screenshot({ path: path.join(screenshots, 'policy-library.png') })
    await page.getByRole('link', { name: '查看版本历史' }).click()
    const versions = page.getByRole('table', { name: '政策版本时间线' })
    await versions.waitFor()
    assert.ok(await versions.locator('tbody tr').count())
    await versions.getByRole('button', { name: '查看详情' }).first().click()
    await page.getByRole('heading', { name: '原始正文' }).waitFor()
    await page.screenshot({ path: path.join(screenshots, 'version-history.png') })
    console.log('PASS policy search, live details and version details')

    await page.getByRole('link', { name: '查看采集记录' }).click()
    const fetches = page.getByRole('table', { name: '采集事件' })
    await fetches.waitFor()
    await fetches.getByRole('link', { name: '查看档案' }).first().click()
    const htmlPreview = page.locator('iframe.dc-preview')
    await htmlPreview.waitFor()
    assert.equal(await htmlPreview.getAttribute('sandbox'), '')
    const htmlBody = htmlPreview.contentFrame().locator('body')
    await htmlBody.waitFor()
    assert.ok((await htmlBody.innerText()).trim().length > 25)
    console.log('PASS fetch records link to sandboxed HTML preview')

    const archive = await visit('archive', '原始档案文件', '原始档案')
    const fixture = archive.getByRole('row').filter({ hasText: '合成文件' })
    await fixture.getByRole('button', { name: '查看预览' }).click()
    const pdfPreview = page.locator('iframe.dc-preview')
    await pdfPreview.waitFor()
    assert.equal(await pdfPreview.getAttribute('sandbox'), '')
    await pdfPreview.contentFrame().getByRole('heading', { name: '第 1 页' }).waitFor()
    await page.screenshot({ path: path.join(screenshots, 'raw-archive.png') })
    console.log('PASS fixture is labeled and opens in the sandboxed PDF preview')

    const reports = await visit('ingestion', '采集运行报告', '采集流水线')
    const run = reports.locator('tbody tr').last()
    await run.getByRole('button').click()
    await page.getByRole('table', { name: '采集运行各阶段结果' }).waitFor()
    await page.screenshot({ path: path.join(screenshots, 'ingestion-pipeline.png') })
    await page.getByRole('checkbox').first().check()
    await page.getByRole('button', { name: '运行离线重放' }).click()
    const replay = page.locator('section').filter({ has: page.getByRole('heading', { name: '本次离线重放' }) }).last()
    await replay.getByRole('heading', { name: '本次离线重放' }).waitFor()
    assert.equal(await replay.locator('dl div').filter({ hasText: '网络请求' }).locator('dd').innerText(), '0')
    console.log('PASS saved ingestion report selection and isolated replay with zero network requests')

    const sources = await visit('sources', '候选采集目标', '数据源注册表')
    assert.ok(await sources.getByText('待审核', { exact: true }).count())
    await page.screenshot({ path: path.join(screenshots, 'source-registry.png') })
    assert.deepEqual(errors, [])
    console.log('PASS source registry and pending candidates')

    await page.goto(baseUrl + '/#/data/policies')
    await page.getByRole('textbox', { name: '搜索标题或正文' }).fill('无匹配词条示例 862533')
    await page.getByRole('button', { name: '搜索', exact: true }).click()
    await page.getByText('没有匹配的政策记录').waitFor()
    await page.route('**/api/workbench/policies?**', route => route.abort('connectionrefused'))
    await page.reload()
    await page.getByText('无法连接工作台 API。请确认后端服务已启动。').waitFor()
    await page.unrouteAll()
    await page.getByRole('button', { name: '重试' }).first().click()
    await page.getByRole('table', { name: '政策列表' }).waitFor()
    console.log('PASS empty results, connection failure and recovery through retry')

    await page.goto(baseUrl + '/#/data/policies?policy_id=' + 'f'.repeat(32))
    await page.getByText('没有找到该政策版本。').waitFor()
    await page.getByRole('button', { name: '关闭政策详情' }).click()
    await page.getByRole('table', { name: '政策列表' }).waitFor()
    assert.equal(await page.getByText('没有找到该政策版本。').count(), 0)
    await page.getByRole('button', { name: title, exact: true }).click()
    await page.getByRole('heading', { name: '原始正文' }).waitFor()
    console.log('PASS missing policy 404 and recovery with a valid selection')

    await page.goto(baseUrl + '/#/data/policies')
    let releaseRequests
    const waitForRequest = new Promise(resolve => { releaseRequests = resolve })
    await page.route('**/api/workbench/policies?**', async route => {
      await waitForRequest
      await route.continue()
    })
    try {
      await page.reload()
      await page.getByRole('status').filter({ hasText: '正在读取' }).first().waitFor()
    } finally {
      releaseRequests()
    }
    await page.getByRole('table', { name: '政策列表' }).waitFor()
    await page.setViewportSize({ width: 1280, height: 800 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    await page.getByRole('textbox', { name: '搜索标题或正文' }).fill(title.slice(0, 4))
    await page.getByRole('button', { name: '搜索', exact: true }).click()
    await page.getByRole('button', { name: title, exact: true }).waitFor()
    assert.deepEqual(errors, [])
    console.log('PASS loading indicator and 1280px laptop layout')

    await page.setViewportSize({ width: 1366, height: 768 })
    await page.goto(baseUrl + '/#/overview')
    await page.getByRole('heading', { name: '系统架构', exact: true }).waitFor()
    await page.locator('.report-row').first().waitFor()
    const response = await page.request.get(baseUrl + '/api/workbench/overview')
    assert.equal(response.status(), 200)
    const { counts } = (await response.json()).data
    const concepts = [['注册数据源', 'sources'], ['在库政策', 'policies'], ['已存版本', 'versions'], ['采集事件', 'fetches']]
    for (const [label, key] of concepts) {
      const value = await page.locator('.metric-card').filter({ has: page.getByText(label, { exact: true }) }).locator('.metric-value').innerText()
      assert.equal(Number(value), counts[key])
    }
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
    await page.setViewportSize({ width: 1440, height: 1024 })
    await page.screenshot({ path: path.join(screenshots, 'overview.png') })
    console.log('PASS overview with real counter values, reports and 1366px layout')

    const rootColors = await page.evaluate(() => {
      const styles = getComputedStyle(document.documentElement)
      return ['--canvas', '--accent', '--secondary'].map(name => styles.getPropertyValue(name).trim().toLowerCase())
    })
    assert.deepEqual(rootColors, ['#00070a', '#00d7ff', '#fc734d'])
    const primaryColor = await page.getByRole('link', { name: '调试 Agent' })
      .evaluate(button => getComputedStyle(button).backgroundColor)
    assert.equal(primaryColor, 'rgb(0, 215, 255)')
    console.log('PASS midnight background, electric-blue primary action and orange accent tokens')

    await page.getByRole('link', { name: '调试 Agent' }).click()
    await page.getByRole('heading', { name: 'Agent Playground', exact: true }).waitFor()
    await page.getByLabel('任务描述').fill('查询电力市场政策并出示原文证据')
    await page.getByLabel('检索关键词').fill('电力市场')
    const taskResponse = page.waitForResponse(request => request.url().endsWith('/api/workbench/runs') && request.request().method() === 'POST')
    await page.getByRole('button', { name: '运行任务' }).click()
    const runtimeResponse = await taskResponse
    assert.equal(runtimeResponse.status(), 200)
    const { data: task } = await runtimeResponse.json()
    assert.equal(task.mode, 'local')
    assert.equal(task.external_model_calls, 0)
    assert.ok(task.tool_calls.some(tool => tool.name === 'search_policy' && tool.execution_status === 'success'))
    assert.ok(task.evidence.length > 0)
    await page.getByRole('heading', { name: '任务结果' }).waitFor()
    await page.locator('summary').filter({ hasText: 'search_policy' }).first().click()
    await page.getByRole('heading', { name: '调用参数' }).first().waitFor()
    await page.getByRole('heading', { name: '政策来源与证据' }).waitFor()
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: path.join(screenshots, 'agent-playground.png') })
    console.log('PASS local Agent executes actual policy tools and renders arguments, results and evidence')

    await page.getByRole('link', { name: '查看 Trace' }).click()
    await page.getByRole('heading', { name: '运行事件时间线' }).waitFor()
    assert.ok(await page.locator('.runtime-id').getByText(task.task_id, { exact: true }).isVisible())
    assert.equal(await page.locator('.trace-list > li').count(), task.trace.events.length)
    await page.screenshot({ path: path.join(screenshots, 'observability.png') })
    console.log('PASS observability renders this task’s own Trace and events')

    await page.goto(baseUrl + '/#/tools')
    await page.getByRole('heading', { name: 'search_policy', exact: true }).waitFor()
    const toolResponse = await page.request.get(baseUrl + '/api/workbench/tools')
    const { data: registered } = await toolResponse.json()
    const toolNames = await page.locator('.tool-grid h2 code').allTextContents()
    assert.deepEqual(toolNames.sort(), registered.items.map(tool => tool.name).sort())
    assert.equal(await page.getByRole('heading', { name: '参数 Schema' }).count(), toolNames.length)
    await page.screenshot({ path: path.join(screenshots, 'tool-registry.png') })
    assert.deepEqual(errors, [])
    console.log('PASS Tool Registry names and schemas match registered tools')
  } finally {
    await browser.close()
  }
}

check().catch(error => {
  console.error(error)
  process.exitCode = 1
})
