import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {bundle} from '@remotion/bundler';
import {renderMedia, selectComposition} from '@remotion/renderer';

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const TEMPLATE_IDS = new Set([
  'causal_chain',
  'contrast_split',
  'process_steps',
  'hierarchy_layers',
  'timeline_path',
  'data_trend',
  'concept_map',
]);

const asObject = (value, label) => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label}必须是对象`);
  }
  return value;
};

const positiveInt = (value, label) => {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed <= 0) throw new Error(`${label}必须是正整数`);
  return parsed;
};

const validateRequest = (payload) => {
  const input = asObject(payload, '渲染请求');
  const renders = input.renders;
  if (!Array.isArray(renders) || renders.length === 0) throw new Error('renders不能为空');
  for (const item of renders) {
    asObject(item, '镜头请求');
    if (!String(item.shot_id || '').trim()) throw new Error('镜头缺少shot_id');
    if (!TEMPLATE_IDS.has(String(item.template_id || ''))) throw new Error(`未知说明模板：${item.template_id || ''}`);
    positiveInt(item.width, 'width');
    positiveInt(item.height, 'height');
    positiveInt(item.fps, 'fps');
    positiveInt(item.duration_frames, 'duration_frames');
    const shotClass = String(item.shot_class || 'explanation');
    if (shotClass === 'mixed_explanation') {
      const videoPath = String(item.digital_human_video_path || '').trim();
      if (!videoPath) throw new Error(`${item.shot_id}缺少数字人本地视频路径`);
    }
  }
  return input;
};

const outputPathFor = (outputDir, shotId) => path.resolve(outputDir, `${shotId}.mp4`);

const safeFileName = (value) => String(value || 'shot').replace(/[^a-zA-Z0-9_-]/g, '_');

const withTimeout = async (promise, timeoutMs, label) => {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label}单镜头渲染超时（${Math.round(timeoutMs / 1000)}秒）`)), timeoutMs);
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
};

async function stageLocalVideos(payload) {
  const runtimeDir = path.join(ROOT, 'public', 'runtime', `${Date.now()}-${process.pid}`);
  await fs.mkdir(runtimeDir, {recursive: true});
  for (const request of payload.renders) {
    const source = String(request.digital_human_video_path || '').trim();
    if (!source) continue;
    const sourcePath = path.resolve(source);
    const stat = await fs.stat(sourcePath).catch(() => null);
    if (!stat?.isFile() || stat.size <= 0) throw new Error(`${request.shot_id}数字人视频不存在或为空：${sourcePath}`);
    const fileName = `${safeFileName(request.shot_id)}${path.extname(sourcePath) || '.mp4'}`;
    await fs.copyFile(sourcePath, path.join(runtimeDir, fileName));
    request.digital_human_video_src = `runtime/${path.basename(runtimeDir)}/${fileName}`;
  }
  return runtimeDir;
}

async function main() {
  const requestPath = process.argv[2];
  if (!requestPath) throw new Error('用法：node render.mjs <render-request.json>');
  const payload = validateRequest(JSON.parse(await fs.readFile(path.resolve(requestPath), 'utf8')));
  const outputDir = path.resolve(String(payload.output_dir || path.join(ROOT, 'out')));
  await fs.mkdir(outputDir, {recursive: true});
  const runtimeDir = await stageLocalVideos(payload);
  try {
    const serveUrl = await bundle({
      entryPoint: path.join(ROOT, 'src', 'entry.jsx'),
      webpackOverride: (config) => config,
    });
    const results = [];
    const concurrency = Math.max(1, Math.min(3, Number(payload.render_concurrency || 3)));
    const perShotTimeoutMs = Math.max(30_000, Number(payload.per_shot_timeout_seconds || 180) * 1000);
    let cursor = 0;
    const worker = async () => {
      while (cursor < payload.renders.length) {
        const request = payload.renders[cursor++];
        const outputLocation = outputPathFor(outputDir, String(request.shot_id));
        const inputProps = {...request};
        const label = String(request.shot_id);
        process.stdout.write(`${JSON.stringify({event: 'started', shot_id: label})}\n`);
        const composition = await selectComposition({serveUrl, id: String(request.template_id).replaceAll('_', '-'), inputProps});
        await withTimeout(renderMedia({
          serveUrl,
          composition,
          inputProps,
          codec: 'h264',
          outputLocation,
          audioCodec: null,
          muted: true,
          // 三个镜头并行时，限制每个 Remotion 实例只占一个渲染并发槽，
          // 避免“外层并行 + 内层默认并发”造成 CPU/Chromium 资源争抢而长时间无输出。
          concurrency: 1,
          overwrite: true,
        }), perShotTimeoutMs, label);
        results.push({
          shot_id: label,
          status: 'succeeded',
          output_path: outputLocation,
          template_id: String(request.template_id),
          width: composition.width,
          height: composition.height,
          fps: composition.fps,
          duration_frames: composition.durationInFrames,
        });
        process.stdout.write(`${JSON.stringify({event: 'completed', shot_id: label})}\n`);
      }
    };
    await Promise.all(Array.from({length: Math.min(concurrency, payload.renders.length)}, () => worker()));
    process.stdout.write(`${JSON.stringify({status: 'succeeded', results})}\n`);
  } finally {
    await fs.rm(runtimeDir, {recursive: true, force: true});
  }
}

main().catch((error) => {
  process.stderr.write(`${error?.stack || error}\n`);
  process.stdout.write(`${JSON.stringify({status: 'failed', error: String(error?.message || error)})}\n`);
  process.exitCode = 1;
});
