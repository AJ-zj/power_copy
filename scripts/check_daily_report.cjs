const assert = require('node:assert/strict')
const fs = require('node:fs/promises')
const path = require('node:path')
const { chromium } = require(process.env.POWERCOPY_PLAYWRIGHT || 'playwright')

const web = process.env.POWERCOPY_WEB_URL || 'http://127.0.0.1:6186'
const project = path.resolve(__dirname, '..')
const workflow = path.join(project, 'storage/workflow/mvp05-mock-verification.json')
const testResults = path.join(project, 'docs/workbench-test-results.json')
const testNote = '日报浏览器验收'

function shanghaiDate(value) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date(value))
}

async function check() {
  const [source, snapshot] = await Promise.all([workflow, testResults].map(async file =>
    JSON.parse(await fs.readFile(file, 'utf8')),
  ))
  const runDay = shanghaiDate(source.created_at)
  const testDay = shanghaiDate(snapshot.verified_at)
  assert.notEqual(runDay, testDay, 'Use a Mock result from a different date than the test snapshot')

  const browser = await chromium.launch({
    executablePath: process.env.POWERCOPY_BROWSER || undefined,
    headless: true,
  })
  try {
    const page = await browser.newPage({ acceptDownloads: true })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.goto(web)
    await page.getByRole('link', { name: '每日报告' }).click()
    await page.getByRole('heading', { name: '每日工作与产出' }).waitFor()
    const day = page.getByLabel('报告日期')
    const title = page.getByText(source.draft.title, { exact: true })
    const note = page.getByLabel('当日完成工作')
    await day.fill(runDay)
    await title.first().waitFor()
    await page.getByText('成本核算服务待接入', { exact: true }).waitFor()
    await note.fill(testNote)

    await day.fill(testDay)
    await page.getByText(snapshot.suites[0].label, { exact: true }).waitFor()
    assert.equal(await title.count(), 0)
    await day.fill(runDay)
    await title.first().waitFor()
    assert.equal(await note.inputValue(), testNote)

    const downloaded = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出日报' }).click()
    const file = await downloaded
    assert.equal(file.suggestedFilename(), 'powercopy-daily-' + runDay + '.md')
    const report = await fs.readFile(await file.path(), 'utf8')
    for (const fragment of [testNote, source.draft.title, source.task_id, '待对账']) {
      assert.ok(report.includes(fragment), fragment)
    }
    if (process.env.POWERCOPY_SCREENSHOTS) {
      const output = path.join(process.env.POWERCOPY_SCREENSHOTS, 'daily-report.png')
      await page.screenshot({ path: output, fullPage: true })
      console.log('SCREENSHOT', output)
    }
    assert.deepEqual(errors, [])
    console.log('PASS', source.task_id, 'real workflow, date filter, manual note and Markdown export')
  } finally {
    await browser.close()
  }
}

check().catch(error => {
  console.error(error)
  process.exitCode = 1
})
