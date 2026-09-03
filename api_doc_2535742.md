查询已训练音色的状态。

&nbsp;

<span data-label="purple">POST</span>`https://openspeech.bytedance.com/api/v3/tts/get_voice`

以下请求头主要为[新版控制台](https://console.volcengine.com/speech/new?_vtm_=a106466.b106468.0_0.0_0.0.844_7636990835414320667)鉴权参考示例，若使用[旧版控制台](https://console.volcengine.com/speech/app)，鉴权参考示例详见：[旧版控制台鉴权参考示例](https://www.volcengine.com/docs/6561/2534847?lang=zh)。<mark>旧版控制台后续会逐步下线，建议尽快切换至</mark>[新版控制台](https://console.volcengine.com/speech/new?_vtm_=a106466.b106468.0_0.0_0.0.844_7636990835414320667)<mark>使用。</mark>


<span id="vsY5GiPG"></span>
### 请求头


**Content\-Type ** `string` <span data-api-tag="require|9gv9Vz">必选</span>

固定值："application/json"



**X\-Api\-Key ** `string` <span data-api-tag="require|9gv9Vz">必选</span>

API Key 可以从 [控制台>API Key管理](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default.) 获取



**X\-Api\-Request\-Id ** `string` <span data-api-tag="require|M22Sxg">必选</span>

标识客户端请求ID，uuid随机字符串



&nbsp;


<span id="JCP4OcxU"></span>
### 请求体


**speaker_id**`string` <span data-api-tag="require|uo2J0a">必选</span>

唯一音色代号，[speaker_id获取参考](https://www.volcengine.com/docs/6561/1167802?lang=zh&_vtm_=a106466.b106468.0_0.0_0.0.902_7636990835414320667#api%E8%B0%83%E7%94%A8%E5%8F%82%E6%95%B0%E8%8E%B7%E5%8F%96)。



**custom_speaker_id**`string` 

**含义**：自定义音色代号（仅支持后付费音色，关于后付费音色下单几说明详见：[《声音复刻下单及使用指南》](https://www.volcengine.com/docs/6561/1167802?lang=zh&_vtm_=a106466.b106468.0_0.0_0.0.970_7636990835414320667)）。

后付费声音复刻示例：

```JSON
{
"speaker_id": "custom_speaker_id", // 固定值
"custom_speaker_id": "custom_zh_xxx" // 训练接口中自定义的音色代号
}
```




<span id="9YkuGgDj"></span>
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

创建时间



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




&nbsp;

&nbsp;



