from runtime import Args
import urllib.request
import urllib.error
import json
import time


class ActiveCrashError(Exception):
    pass


def handler(args: Args) -> dict:
    start_time = time.time()
    ABSOLUTE_DEADLINE = 172.0
    POLL_INTERVAL = 30.0

    task_id = ""
    api_key = ""

    if hasattr(args.input, "task_id"):
        task_id = args.input.task_id
        api_key = getattr(args.input, "api_key", "")
    elif hasattr(args.input, "get"):
        task_id = args.input.get("task_id", "")
        api_key = args.input.get("api_key", "")

    task_id = str(task_id).strip().strip("'").strip('"')
    api_key = str(api_key).strip().strip("'").strip('"')

    if not task_id:
        return {"error": True, "message": "缺失 task_id"}
    if not api_key:
        return {"error": True, "message": "缺失 api_key"}

    if not api_key.startswith("Bearer "):
        api_key = f"Bearer {api_key}"

    url = f"https://api.aishuch.com/v1/tasks/{task_id}"
    headers = {"Authorization": api_key, "User-Agent": "Mozilla/5.0"}

    attempt = 0
    while True:
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=10) as response:
                raw_result = json.loads(response.read().decode("utf-8"))

                data_obj = raw_result.get("data", {})
                status = data_obj.get("status", "unknown")

                if status == "completed":
                    try:
                        image_url = data_obj["result"]["images"][0]["url"][0]
                    except:
                        image_url = ""
                    elapsed_time = time.time() - start_time
                    return {
                        "code": 200,
                        "status": "completed",
                        "image_url": image_url,
                        "msg": f"成功！共查询 {attempt+1} 次，总耗时 {elapsed_time:.1f} 秒",
                    }

                elif status == "failed":
                    error_msg = data_obj.get("error", {}).get("message", "未知错误")
                    return {
                        "code": 500,
                        "status": "failed",
                        "message": f"绘图失败：{error_msg}",
                    }

                else:
                    current_elapsed = time.time() - start_time
                    if current_elapsed >= ABSOLUTE_DEADLINE:
                        raise ActiveCrashError(
                            f"【极限熔断】已给足 {current_elapsed:.1f} 秒，最后一次查询仍未完成，立刻重启节点！"
                        )

                    remaining_time = ABSOLUTE_DEADLINE - current_elapsed
                    sleep_time = min(POLL_INTERVAL, remaining_time)
                    time.sleep(sleep_time)
                    attempt += 1

        except ActiveCrashError as crash_err:
            raise crash_err
        except urllib.error.HTTPError as e:
            return {
                "error": True,
                "status_code": e.code,
                "raw_response": e.read().decode("utf-8")[:300],
            }
        except Exception as e:
            time.sleep(2)
            if time.time() - start_time >= ABSOLUTE_DEADLINE:
                raise ActiveCrashError("【极限熔断】网络抖动重试期间超时，立刻重启节点！")
