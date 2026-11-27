const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { chromium } = require(process.env.POWERCOPY_PLAYWRIGHT || 'playwright')

const web = process.env.POWERCOPY_WEB_URL || 'http://127.0.0.1:6184'
const screenshots = process.env.POWERCOPY_SCREENSHOTS || path.resolve(__dirname, '../docs/screenshots')

async function check() {
  await fs.mkdir(screenshots, { recursive: true })
  const browser = await chromium.launch({
    executablePath: process.env.POWERCOPY_BROWSER || undefined,
    headless: true,
  })

  try {
    const context = await browser.newContext({
      viewport: { width: 1600, height: 1200 },
      acceptDownloads: true,
    })
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', error => errors.push(error.message))

    await page.goto(`${web}/#/content`)
    await page.getByRole('heading', { name: 'Content Studio', exact: true }).waitFor()
    await page.getByText('就绪', { exact: true }).waitFor()

    await page.getByLabel('文案主题').fill('工商业企业参与电力市场有哪些基本规则')
    await page.getByLabel('目标客户').fill('工商业用电企业')
    await page.getByLabel('目标地区').selectOption({ label: '安徽' })
    await page.getByLabel('目标字数').fill('300')

    const responsePromise = page.waitForResponse(response =>
      response.url().endsWith('/api/workbench/content/runs') && response.request().method() === 'POST',
      { timeout: 120000 },
    )
    await page.getByRole('button', { name: '开始创作' }).click()
    const response = await responsePromise
    assert.equal(response.status(), 200, await response.text())
    const run = (await response.json()).data
    assert.equal(run.generation_mode, 'mock')
    assert.equal(run.requires_human, true)
    assert.ok(run.evidence_package.evidence_items.length > 0)
    assert.ok(run.content_draft)
    assert.ok(run.review_report)
    assert.deepEqual(run.review_report.dimension_results.map(item => item.dimension).sort(),
      ['applicability', 'citation', 'compliance', 'fact_support'])
    assert.deepEqual(run.revision_records, run.revision ? [run.revision] : [])
    assert.ok(run.budget.used_api_attempts > 0)
    assert.ok(run.budget.used_api_attempts <= run.budget.max_api_attempts)

    await page.getByText(run.task_id, { exact: true }).waitFor()
    const evidenceId = run.content_draft.key_points[0].evidence_ids[0]
    const evidence = run.evidence_package.evidence_items.find(item => item.evidence_id === evidenceId)
    assert.ok(evidence)
    await page.getByRole('button', { name: `查看引用 ${evidenceId.slice(0, 6)}` }).first().click()
    const openEvidence = page.locator(`[id="content-evidence-${evidenceId}"][open]`)
    await openEvidence.locator('blockquote').waitFor()
    assert.equal(await openEvidence.locator('blockquote').textContent(), evidence.exact_quote)
    const review = page.getByRole('region', { name: '审核记录' })
    await review.waitFor()

    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: path.join(screenshots, 'content-studio-mock.png'), fullPage: true })

    const editor = page.getByLabel('文案正文')
    const editedBody = `${run.content_draft.body}\n页面验收修改。`
    await editor.fill(editedBody)
    await page.getByText(/草稿已修改，旧审核结果和证据映射可能失效/).waitFor()
    await page.getByText('编辑后的草稿未审核。').waitFor()
    assert.equal(await review.count(), 0)

    await page.getByRole('button', { name: '复制草稿' }).click()
    await page.getByText('草稿已复制到剪贴板。').waitFor()
    const clipboard = await page.evaluate(() => navigator.clipboard.readText())
    const editedDraft = `${run.content_draft.title}\n\n${editedBody}`
    assert.equal(clipboard.replace(/\r\n/g, '\n'), editedDraft)

    const downloadPromise = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出草稿' }).click()
    const download = await downloadPromise
    assert.equal(download.suggestedFilename(), `${run.content_draft.draft_id}.txt`)
    const downloadPath = await download.path()
    const exported = await fs.readFile(downloadPath, 'utf8')
    assert.equal(exported, editedDraft)
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: path.join(screenshots, 'content-studio-edited.png'), fullPage: true })

    assert.deepEqual(errors, [])
    console.log(`PASS Mock run ${run.task_id}: ${run.status}, ${run.evidence_package.evidence_items.length} evidence items, ${run.budget.used_api_attempts}/${run.budget.max_api_attempts} attempts`)
    console.log('PASS evidence quote, review dimensions, edit invalidation, clipboard and text export')
    console.log(`SCREENSHOT ${path.join(screenshots, 'content-studio-mock.png')}`)
    console.log(`SCREENSHOT ${path.join(screenshots, 'content-studio-edited.png')}`)
    await context.close()
  } finally {
    await browser.close()
  }
}

check().catch(error => {
  console.error(error)
  process.exitCode = 1
})
