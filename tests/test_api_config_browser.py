"""Real local browser regression; never starts the shared service or vendors."""

import os
import subprocess
import threading
from pathlib import Path

from topic_migration.config_service import ConfigService
from topic_migration.server import create_server
from topic_migration.topic_service import TopicCenterService


def test_api_config_browser_save_types_conflicts_and_reload(tmp_path):
    runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    node = runtime / "bin/node.exe"
    assert node.is_file(), "Browser acceptance requires the installed Node runtime"
    service = TopicCenterService(tmp_path / "runtime")
    config = service.config_service
    config.save({"channel_id": "digital-human", "settings": {"endpoint": "https://target.example", "enabled": False}})
    config.merge_import({"digital-human": {"settings": {"endpoint": "https://source.example"}}})
    server = create_server("127.0.0.1", 0, service=service, lock_path=tmp_path / "service.lock")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    script = r"""
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext();
    await context.route('**/*',route=>new URL(route.request().url()).origin===process.env.API_TEST_BASE?route.continue():route.abort());
    const page=await context.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(process.env.API_TEST_BASE+'/api-management');
    await page.locator('#provider option[value="digital-human"]').waitFor({state:'attached'});
    await page.selectOption('#provider','digital-human');
    await page.locator('aside .status.warn').waitFor();
    await page.locator('[data-setting="primary_model"]').fill('synthetic-application-id');
    await page.locator('[data-setting="enabled"]').check();
    const saved=page.waitForResponse(r=>r.url().endsWith('/api/api-management/config'));
    await page.locator('button[type="submit"]').click();
    assert.equal((await saved).status(),200);
    await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('已保存'));
    await page.reload();
    await page.locator('#provider option[value="digital-human"]').waitFor({state:'attached'});
    await page.selectOption('#provider','digital-human');
    assert.equal(await page.locator('[data-setting="primary_model"]').inputValue(),'synthetic-application-id');
    assert.equal(await page.locator('[data-setting="enabled"]').isChecked(),true);
    await page.locator('aside .status.warn').waitFor();
    await page.setViewportSize({width:1440,height:1000});
    await page.screenshot({path:process.env.API_TEST_OUTPUT+'/desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:process.env.API_TEST_OUTPUT+'/mobile.png',fullPage:true});
        assert.deepEqual(errors,[]);
    console.log('browser save, reload, boolean, numeric, missing fields, conflict display, desktop/mobile: passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    try:
        env = {**os.environ, "NODE_PATH": str(runtime / "node_modules"), "API_TEST_BASE": base, "API_TEST_OUTPUT": str(tmp_path)}
        result = subprocess.run([str(node), "-e", script], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        fresh = ConfigService(config.root).public_snapshot()
        rows = {row["channel_id"]: row for row in fresh["groups"]}
        assert rows["digital-human"]["settings"]["enabled"] is True
        assert rows["digital-human"]["config_status"] == "import_conflict"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_api_config_browser_provider_card_switches_current_service(tmp_path):
    runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    node = runtime / "bin/node.exe"
    assert node.is_file(), "Browser acceptance requires the installed Node runtime"
    service = TopicCenterService(tmp_path / "runtime")
    config = service.config_service
    public_snapshot = config.public_snapshot
    def snapshot_with_shared_tts_key():
        data = public_snapshot()
        groups = {group["channel_id"]: group for group in data["groups"]}
        groups["tts"]["credential_status"] = "configured"
        groups["sound-effect"]["credential_status"] = "configured"
        groups["sound-effect"]["credential_source"] = "tts.primary_api_key"
        return data
    config.public_snapshot = snapshot_with_shared_tts_key
    server = create_server("127.0.0.1", 0, service=service, lock_path=tmp_path / "service.lock")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    script = r"""
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext();
    const requests=[];
    await context.route('**/*',route=>{
      const request=route.request();
      if (new URL(request.url()).origin===process.env.API_TEST_BASE) {
        requests.push({method:request.method(),url:request.url()});
        return route.continue();
      }
      return route.abort();
    });
    const page=await context.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(process.env.API_TEST_BASE+'/api-management');
    await page.locator('[data-category-id="video-generation"]').waitFor({state:'visible'});
    assert.deepEqual(await page.locator('[data-category-id]').allTextContents(),['大语言模型','生图模型','生视频模型','TOS','TTS','语音复刻','音效生成','其他服务']);
    assert.equal(await page.locator('#provider').inputValue(),'language-model');
    assert.equal(await page.locator('[data-channel-id="story-writing"]').count(),1,JSON.stringify({html:await page.locator('#providers').innerHTML(),errors}));
    assert.equal(await page.locator('[data-channel-id="story-writing"]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('[data-channel-id="director-seed21"]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('[data-channel-id="visual-guidance"]').count(),0);
    assert.equal(await page.locator('.legacy-channel summary').textContent(),'历史通道：编导视觉约束（visual-guidance）');
    await page.locator('#category-tab-language-model').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#category-tab-image-generation').getAttribute('aria-selected'),'true');
    await page.locator('[data-category-id="video-generation"]').click();
    assert.equal(await page.locator('#provider').inputValue(),'video-generation');
    assert.equal(await page.locator('#configGrid').getAttribute('aria-labelledby'),'category-tab-video-generation');
    await page.locator('[data-channel-id="digital-human"]').waitFor({state:'visible'});
    const digital=page.locator('[data-channel-id="digital-human"]');
    const digitalName=(await digital.locator('h4').textContent()).trim();
    await digital.click();
    assert.equal(await page.locator('#provider').inputValue(),'digital-human');
    assert.equal(await page.locator('#groupId').inputValue(),'digital-human');
    assert.equal((await page.locator('.config-grid article h3').textContent()).trim(),digitalName);
    assert.equal(await digital.getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('[data-channel-id="story-writing"]').count(),0);
    await page.locator('[data-category-id="other-services"]').click();
    assert.equal(await page.locator('#configGrid').getAttribute('aria-labelledby'),'category-tab-other-services');
    await page.locator('[data-channel-id="tikhub"]').click();
    assert.equal(await page.locator('#provider').inputValue(),'tikhub');
    assert.equal(await page.locator('[data-channel-id="tikhub"]').getAttribute('aria-pressed'),'true');
    await page.locator('[data-category-id="voice-clone"]').click();
    assert.equal(await page.locator('#provider').inputValue(),'voice-clone');
    assert.equal(await page.locator('#voiceClonePanel').isVisible(),true);
    assert.match(await page.locator('#voiceCloneStatus').textContent(),/复用 TTS 主凭据/);
    assert.match(await page.locator('#voiceCloneStatus').textContent(),/复刻接口未单独验证/);
    assert.match(await page.locator('#voiceCloneStatus').textContent(),/已存在/);
    assert.equal(await page.locator('#voiceClonePanel input[type="password"]').count(),0);
    await page.locator('#openTtsSettings').click();
    assert.equal(await page.locator('#provider').inputValue(),'tts');
    await page.locator('[data-category-id="sound-effect"]').click();
    assert.equal(await page.locator('#provider').inputValue(),'sound-effect');
    assert.match(await page.locator('#apiKey').evaluate(input=>input.closest('.field').querySelector('small').textContent),/留空时复用 TTS 主 Key/);
    assert.equal(await page.locator('#apiKey').getAttribute('placeholder'),'当前复用 TTS 主 Key；留空继续复用');
    assert.match(await page.locator('#connection').textContent(),/使用 TTS 主 Key/);
    await page.setViewportSize({width:390,height:844});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    assert.equal(await page.locator('.topnav a').evaluateAll(links=>links.every(link=>link.getBoundingClientRect().height<=40)),true);
    assert.equal(requests.filter(request=>request.method==='POST').length,0);
    assert.deepEqual(errors,[]);
    console.log('category tabs, service switch, shared TTS credentials, no save request: passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    try:
        env = {**os.environ, "NODE_PATH": str(runtime / "node_modules"), "API_TEST_BASE": base}
        result = subprocess.run([str(node), "-e", script], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_api_config_browser_unified_language_model_settings_save_and_reload(tmp_path):
    runtime = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node"
    node = runtime / "bin/node.exe"
    assert node.is_file(), "Browser acceptance requires the installed Node runtime"
    service = TopicCenterService(tmp_path / "runtime")
    config = service.config_service
    config.save({"channel_id": "story-writing", "settings": {"endpoint": "https://story.example/v1", "model_slots": {"writer": "ep-writer"}, "model_names": {"writer": "Writer"}, "model_order": ["writer"], "strategy": "primary_then_backup", "max_attempts": 3, "timeout_seconds": 120}})
    config.save({"channel_id": "director-seed21", "settings": {"endpoint": "https://director.example/v1", "model_slots": {"director": "ep-director"}, "model_names": {"director": "Director"}, "model_order": ["director"], "strategy": "primary_then_backup", "max_attempts": 2, "timeout_seconds": 90}})
    config.save({"channel_id": "visual-guidance", "settings": {"endpoint": "https://visual.example/v1", "primary_model": "visual-model"}})
    server = create_server("127.0.0.1", 0, service=service, lock_path=tmp_path / "service.lock")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    script = r"""
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true});
  try {
    const context=await browser.newContext();
    const requests=[];
    await context.route('**/*',route=>{
      const request=route.request();
      if (new URL(request.url()).origin===process.env.API_TEST_BASE) { requests.push({method:request.method(),url:request.url()}); return route.continue(); }
      return route.abort();
    });
    const page=await context.newPage(), errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    await page.goto(process.env.API_TEST_BASE+'/api-management');
    await page.locator('#languageModelPanel').waitFor({state:'visible'});
    assert.equal(await page.locator('[data-lm-channel]').count(),2);
    assert.equal(await page.locator('[data-lm-test]').count(),2);
    assert.deepEqual(await page.locator('[data-lm-test]').allTextContents(),['只读验证连接','只读验证连接']);
    assert.equal(await page.locator('.legacy-channel summary').textContent(),'历史通道：编导视觉约束（visual-guidance）');
    assert.equal(await page.locator('[data-lm-channel="story-writing"] [data-lm-slot-value]').inputValue(),'ep-writer');
    const verification=page.waitForResponse(r=>r.url().endsWith('/api/api-management/test-connection'));
    await page.locator('[data-lm-channel="story-writing"] [data-lm-test]').click();
    const verificationResponse=await verification;
    assert.equal(verificationResponse.status(),200);
    assert.equal((await verificationResponse.json()).external_requests,false);
    await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('凭据缺失'));
    await page.locator('[data-lm-channel="story-writing"] [data-lm-slot-value]').fill('ep-writer-updated');
    await page.locator('[data-lm-channel="director-seed21"] [data-lm-setting="max_attempts"]').fill('4');
    const responses=[];
    page.on('response',response=>{if(response.url().endsWith('/api/api-management/config'))responses.push(response.status())});
    await page.locator('#saveLanguageModels').click();
    await page.locator('#languageModelStatus').getByText('已保存 2 个语言模型通道').waitFor();
    assert.deepEqual(responses,[200,200]);
    await page.reload();
    await page.locator('#languageModelPanel').waitFor({state:'visible'});
    assert.equal(await page.locator('[data-lm-channel="story-writing"] [data-lm-slot-value]').inputValue(),'ep-writer-updated');
    assert.equal(await page.locator('[data-lm-channel="director-seed21"] [data-lm-setting="max_attempts"]').inputValue(),'4');
    assert.equal(await page.locator('[data-lm-channel="visual-guidance"]').count(),0);
    assert.equal(requests.filter(request=>request.method==='POST').length,3);
    assert.deepEqual(errors,[]);
    console.log('unified language-model settings, save, reload, and no external request: passed');
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    try:
        env = {**os.environ, "NODE_PATH": str(runtime / "node_modules"), "API_TEST_BASE": base}
        result = subprocess.run([str(node), "-e", script], env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
