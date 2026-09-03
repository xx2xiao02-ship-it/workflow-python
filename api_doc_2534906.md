上传语音样本，训练自定义语音音色，用于语音合成。

&nbsp;

<span data-label="purple">POST</span>`https://openspeech.bytedance.com/api/v3/tts/voice_clone`

以下请求头主要为[新版控制台](https://console.volcengine.com/speech/new?_vtm_=a106466.b106468.0_0.0_0.0.844_7636990835414320667)鉴权参考示例，若使用[旧版控制台](https://console.volcengine.com/speech/app)，鉴权参考示例详见：[旧版控制台鉴权参考示例](https://www.volcengine.com/docs/6561/2534847?lang=zh)。<mark>旧版控制台后续会逐步下线，建议尽快切换至</mark>[新版控制台](https://console.volcengine.com/speech/new?_vtm_=a106466.b106468.0_0.0_0.0.844_7636990835414320667)<mark>使用。</mark>


<span id="oyvg03nq"></span>
### 请求头


**Content\-Type ** `string` <span data-api-tag="require|9gv9Vz">必选</span>

固定值："application/json"



**X\-Api\-Key ** `string` <span data-api-tag="require|9gv9Vz">必选</span>

API Key 可以从 [控制台>API Key管理](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default.) 获取



**X\-Api\-Request\-Id ** `string` <span data-api-tag="require|M22Sxg">必选</span>

标识客户端请求ID，uuid随机字符串



&nbsp;


<span id="QtgDzszh"></span>
### 请求体


**speaker_id**`string` <span data-api-tag="require|uo2J0a">必选</span>

唯一音色代号，[speaker_id获取参考](https://www.volcengine.com/docs/6561/1167802?lang=zh&_vtm_=a106466.b106468.0_0.0_0.0.902_7636990835414320667#api%E8%B0%83%E7%94%A8%E5%8F%82%E6%95%B0%E8%8E%B7%E5%8F%96)。



**custom_speaker_id**`string` 

**含义**：自定义音色代号（仅支持后付费音色，关于后付费音色下单及说明详见：[《声音复刻下单及使用指南》](https://www.volcengine.com/docs/6561/1167802?lang=zh&_vtm_=a106466.b106468.0_0.0_0.0.970_7636990835414320667)）。

**<mark>注意</mark>**<mark>：首次调用合成接口即视为 “转正” 并</mark>**<mark>收取音色槽位费</mark>**<mark>，请务必在确认试听效果满意后再进行正式合成！</mark>

<span id="3OOuXUnS"></span>
### 一、 参数调用格式

使用自定义音色时，speaker_id 必须传固定值，实际自定义名称写在 custom_speaker_id 中：

```JSON
{
"speaker_id": "custom_speaker_id",   // 必须为固定值
"custom_speaker_id": "custom_zh_xxx" // 客户自定义的音色代号
}
```


<span id="Q4eq3xNa"></span>
### 二、 命名规范


* **字符与长度**：8 ~ 256 个字符，仅支持数字、大小写字母、中划线 \-、下划线 _。

* **首尾限制**：**必须以英文字母开头**；首位和末位不可为 \- 或 _。

* **唯一性**：同 accountID 维度下不可与已有 ID 重复。

* **官方防冲突**：不可与官方精品音色重名。系统会自动拦截特定的官方前缀（如 S_、MIX_）或后缀（如 _tob、_streaming）的名称。

   * 如果你的命名在[正则表达式验证网站](https://regex101.com/)匹配以下正则表达式，将被系统拦截（表示与官方音色冲突或格式不符）：


```Go
`^((?i:S_|ICL_|MIX_|DiT_|BV)|[a-z]{2}_|(?i:(wvae|moon|mercury|venus|earth|mars|jupiter|saturn|uranus|neptune|pluto|umm)_)).*|.*_(?i:bigtts|bigtts_cc|tob|cs_tob|streaming)$|^[^a-zA-Z]|.*[-_]$|^.{0,7}$|^.{257,}$|.*[^a-zA-Z0-9_-].*`
```


<span id="FCals6vl"></span>
### 三、 生命周期与计费说明

详细计费政策参见[《声音复刻下单及使用指南》](https://www.volcengine.com/docs/6561/1167802?lang=zh&_vtm_=a106466.b106468.0_0.0_0.0.970_7636990835414320667)



**audio ** `object` ** ** <span data-api-tag="require|PWufDx">必选</span>

音频格式支持：wav、mp3、ogg、m4a、aac、pcm，<mark>其中pcm仅支持24k，单通道</mark>

目前限制文件上传最大10MB


**data ** `string` ** ** <span data-api-tag="require|A9avVV">必选</span>

进制音频字节，需对二进制音频进行base64编码



**format ** `string` ** ** <span data-api-tag="require|A9avVV">必选</span>

<mark>音频格式，pcm、m4a必传</mark>，mp3、ogg、m4a、aac等其他格式可以不指定




**text**`string` 

参考文本，可让用户按照该文本念诵，服务会对比音频与该文本的差异，若差异过大会复刻失败并返回45001109 WERError。



**language** `int`

支持以下语种：

音频内容需要和语种一致


* cn = 0：中文（默认）

* en = 1：英文

* ja = 2：日语

* es = 3：西班牙语

* id = 4：印尼语

* pt = 5：葡萄牙语

* de = 6:  德语

* fr = 7: 法语

* ko = 8：韩语

* it = 9: 意大利语

* th = 10: 泰语

* vi = 11: 越南语

* ru = 12: 俄语

* fil = 13: 菲律宾语

* ms = 14: 马来语

* ar = 15: 阿拉伯语

* mx = 16: 墨西哥西班牙语

* pt\-br = 17: 巴西葡萄牙语

* pl = 19：波兰语

* tr = 20：土耳其语

* sv = 21：瑞典语


**豆包端到端实时语音模型**，支持以下语种：


* cn = 0：中文（默认）

* en = 1：英文



**extra_params ** `object` ** ** 

配置附加参数


**demo_text** `string`

试听文本，长度在4和300字之间，如果指定了语种需要传入对应语种的文本，否则会合成失败。

**注意事项：** demo_text 文本越长，注册耗时越长，建议合理控制文本长度。



**enable_audio_denoise ** `bool`

是否开启降噪（默认False）。开启降噪可能会对声音细节有一定影响，**音频样本噪声较大的情况下建议开启降噪**，音频样本质量较好的情况下建议关闭降噪。



**disable_volume_normalization ** `bool`

是否关闭音量归一化（默认值为 false）。开启音量归一化，合成时是相对统一的音量，如果关闭音量归一化，合成出来的音量会和 prompt 更接近，和 prompt音频相似度也会更高。




<span id="BRifM1P1"></span>
# 响应


<span id="2phtbtYG"></span>
### 响应


**X\-Tt\-Logid ** `string`

服务端返回的 logid，用于在咨询或者反馈时定位问题



**code** `int`

请求状态码。请求失败时，HTTP 状态码不为 200，详情请参见[错误码参考文档](https://www.volcengine.com/docs/6561/2534853?lang=zh#ad7BnUTK)。



**message ** `string`

请求状态信息。训练失败时，会返回对应的失败说明，详情请参见[错误码参考文档](https://www.volcengine.com/docs/6561/2534853?lang=zh#ad7BnUTK)。



**available_training_times** `int`

该speaker_id剩余训练次数



**create_time ** `int`

创建时间，毫秒级时间戳



**language ** `int`

以下为语种对应的枚举值


* cn = 0：中文（默认）

* en = 1：英文

* ja = 2：日语

* es = 3：西班牙语

* id = 4：印尼语

* pt = 5：葡萄牙语

* de = 6:  德语

* fr = 7: 法语

* ko = 8：韩语

* it = 9: 意大利语

* th = 10: 泰语

* vi = 11: 越南语

* ru = 12: 俄语

* fil = 13: 菲律宾语

* ms = 14: 马来语

* ar = 15: 阿拉伯语

* mx = 16: 墨西哥西班牙语

* pt\-br = 17: 巴西葡萄牙语

* pl = 19：波兰语

* tr = 20：土耳其语

* sv = 21：瑞典语



**speaker_id ** `string`

唯一音色代号



**status ** `int`

训练状态，状态为2或4时都可以调用TTS语音合成接口。


* NotFound = 0

* Training = 1

* Success = 2

* Failed = 3

* Active = 4



**speaker_status**`object list`


**model_type ** `int`

复刻 2.0：`model_type = 5` 



**demo_audio ** `string`

试听音频。Success状态时返回，一小时有效，若需要，请下载后使用






