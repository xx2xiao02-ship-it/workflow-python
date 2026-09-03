import test from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

test('未知模板会被阻断', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'remotion-explanation-'));
  const request = path.join(dir, 'request.json');
  fs.writeFileSync(request, JSON.stringify({output_dir: dir, renders: [{shot_id: 'x', template_id: 'unknown', width: 1920, height: 1080, fps: 30, duration_frames: 30}]}));
  const result = spawnSync(process.execPath, [path.resolve('render.mjs'), request], {encoding: 'utf8'});
  assert.notEqual(result.status, 0);
  assert.match(result.stdout, /failed/);
});
