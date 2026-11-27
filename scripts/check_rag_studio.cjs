const assert = require('node:assert/strict')
const path = require('node:path')
const { mkdir } = require('node:fs/promises')
const { chromium } = require(process.env.POWERCOPY_PLAYWRIGHT || 'playwright')

const web = process.env.POWERCOPY_WEB_URL || 'http://127.0.0.1:5174'
const emptyWeb = process.env.POWERCOPY_RAG_MISSING_WEB_URL || 'http://127.0.0.1:5175'
const screenshots = process.env.POWERCOPY_SCREENSHOTS || path.resolve(__dirname, '../docs/screenshots')
const api = '/api/workbench'

async function check() {
  await mkdir(screenshots, { recursive: true })
  const browser = await chromium.launch({
    executablePath: process.env.POWERCOPY_BROWSER || undefined,
    headless: true,
  })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))

    async function read(endpoint, base = web) {
      const response = await page.request.get(base + api + endpoint)
      assert.equal(response.status(), 200, endpoint)
      return (await response.json()).data
    }

    async function search(query, mode, topK = 3) {
      await page.getByRole('searchbox', { name: '检索问题' }).fill(query)
      await page.getByLabel('检索模式').selectOption(mode)
      await page.getByLabel('Top K', { exact: true }).fill(String(topK))
      const responsePromise = page.waitForResponse(response =>
        response.url().endsWith(api + '/rag/search') && response.request().method() === 'POST',
        { timeout: 90000 },
      )
      await page.getByRole('button', { name: '运行检索', exact: true }).click()
      const response = await responsePromise
      assert.equal(response.status(), 200, await response.text())
      const data = (await response.json()).data
      assert.equal(data.mode, mode)
      assert.equal(data.top_k, topK)
      assert.equal(data.candidates_are_not_facts, true)
      assert.ok(Number.isFinite(data.elapsed_ms))
      return data
    }

    await page.goto(web)
    await page.getByRole('link', { name: 'RAG Studio' }).click()
    await page.getByRole('heading', { name: 'RAG Studio', exact: true }).waitFor()
    const status = await read('/rag/status')
    assert.ok(status.documents > 0 && status.chunks > 0)
    assert.equal(status.freshness.status, 'ok')
    assert.equal(status.indexes.lexical.status, 'ready')
    assert.equal(status.indexes.dense.status, 'ready')
    assert.ok(status.indexes.dense.dimension > 0)
    await page.getByText(status.indexes.dense.model, { exact: true }).waitFor()

    const exact = await search('电力市场运行基本规则', 'lexical')
    assert.equal(exact.status, 'success')
    assert.ok(exact.candidates[0].text.includes('电力市场运行基本规则'))
    assert.equal(exact.candidates[0].contributions[0].path, 'lexical')
    assert.equal(exact.candidates[0].contributions[0].rrf, null)
    console.log('PASS 真实索引状态、词法精确查询')

    const semanticQuery = '参与电力市场需要先办理哪些手续？'
    const semanticLexical = await search(semanticQuery, 'lexical')
    assert.equal(semanticLexical.status, 'empty')
    const semanticDense = await search(semanticQuery, 'dense')
    assert.equal(semanticDense.status, 'success')
    assert.ok(semanticDense.candidates.some(candidate => candidate.text.includes('办理市场注册')))
    assert.ok(semanticDense.candidates.every(candidate => candidate.contributions[0].path === 'dense'))
    console.log('PASS 同一语义问题的词法空结果与向量候选')

    const hybrid = await search('电力市场运行基本规则', 'hybrid')
    assert.equal(hybrid.status, 'success')
    assert.ok(hybrid.candidates.some(candidate => candidate.contributions.length === 2))
    assert.ok(hybrid.candidates[0].contributions.every(contribution => contribution.rrf > 0))
    await page.locator('.rag-candidate').first().getByText('查看片段和检索贡献').click()
    const first = hybrid.candidates[0]
    const candidate = page.locator('.rag-candidate').first()
    assert.equal(await candidate.locator('blockquote').textContent(), first.text)
    assert.ok((await candidate.getByRole('link', { name: '查看政策原文' }).getAttribute('href')).includes(first.policy_id))
    await candidate.getByRole('link', { name: '访问官方来源' }).waitFor()
    assert.match(await candidate.getByRole('link', { name: '访问官方来源' }).getAttribute('href'), /^https?:\/\//)
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.setViewportSize({ width: 1440, height: 1650 })
    await page.screenshot({ path: path.join(screenshots, 'rag-studio.png') })
    console.log('PASS Hybrid 双路贡献、候选全文及来源')

    await candidate.getByRole('link', { name: '查看政策原文' }).click()
    await page.getByRole('heading', { name: '原始正文' }).waitFor()
    assert.ok(page.url().includes('version=' + first.version))
    await page.getByRole('link', { name: 'RAG Studio' }).click()
    console.log('PASS 对应版本的政策正文跳转')

    const insufficient = '安徽电力现货市场交易规则'
    const empty = await search(insufficient, 'lexical')
    assert.equal(empty.status, 'empty')
    assert.deepEqual(empty.candidates, [])
    await page.getByText('没有返回候选证据').waitFor()
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: path.join(screenshots, 'rag-studio-empty.png'), fullPage: true })
    const weak = await search(insufficient, 'hybrid')
    assert.equal(weak.status, 'success')
    assert.ok(weak.candidates.length)
    await page.getByText('检索结果为候选片段，引用前请核对政策原文、适用范围和有效性。').waitFor()
    console.log('PASS 证据不足查询的正常空结果与候选警示')

    const benchmark = await read('/rag/benchmark')
    assert.equal(benchmark.status, 'ready')
    assert.equal(benchmark.kind, 'development')
    assert.ok(benchmark.modes.hybrid.at_10)
    await page.getByLabel('评测截断位置').selectOption('at_5')
    await page.getByText('混合 · RRF · 分类型结果', { exact: true }).click()
    const table = page.getByRole('table', { name: '混合 · RRF 分类型指标' })
    await table.waitFor()
    assert.ok(await table.getByRole('row').count() > 1)
    await page.locator('section.panel').filter({ has: page.getByRole('heading', { name: '开发评测基准' }) })
      .screenshot({ path: path.join(screenshots, 'rag-studio-benchmark.png') })
    console.log('PASS 开发评测报告、Top-K 和题型分组')

    const task = await page.request.post(web + api + '/runs', {
      data: { task: '核对政策候选', keyword: semanticQuery, use_retrieval: true, mode: 'local' },
      timeout: 90000,
    })
    assert.equal(task.status(), 200)
    const actual = (await task.json()).data
    assert.equal(actual.external_model_calls, 0)
    assert.equal(actual.status, 'completed')
    const tool = actual.tool_calls.find(call => call.name === 'search_policy_evidence')
    assert.ok(tool && tool.business_status === 'success')
    assert.equal(tool.result.candidates_are_not_facts, true)
    assert.ok(tool.result.evidence_candidates.length)
    console.log('PASS Agent Runtime 经真实工具检索')

    const bounds = await page.request.post(web + api + '/rag/search', {
      data: { query: '电力', mode: 'dense', top_k: 21 },
    })
    assert.equal(bounds.status(), 422)
    assert.equal((await bounds.json()).error.code, 'INVALID_INPUT')

    await page.goto(emptyWeb + '/#/rag')
    const absent = await read('/rag/status', emptyWeb)
    assert.ok(Object.values(absent.indexes).every(index => index.status === 'missing'))
    await page.getByText('暂无开发评测报告').waitFor()
    const failed = await search('电力市场', 'hybrid')
    assert.equal(failed.status, 'failed')
    assert.deepEqual(failed.candidates, [])
    assert.ok(failed.code)
    await page.locator('.rag-outcome[role="alert"]').waitFor()
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: path.join(screenshots, 'rag-studio-index-unavailable.png'), fullPage: true })
    console.log('PASS 参数校验、真实缺索引后端、缺报告与异常状态')

    await page.setViewportSize({ width: 1366, height: 900 })
    await page.goto(web + '/#/roadmap/rag')
    await page.getByRole('heading', { name: 'RAG Studio', exact: true }).waitFor()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth))
    assert.deepEqual(errors, [])
    console.log('PASS 导航兼容入口、1366px 布局、无页面异常')
  } finally {
    await browser.close()
  }
}

check().catch(error => {
  console.error(error)
  process.exitCode = 1
})
