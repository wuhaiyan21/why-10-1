#!/usr/bin/env python3
import argparse
import io
import json
import locale
import os
import sys
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def fix_stdout_encoding():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    pref_enc = locale.getpreferredencoding(False) or "utf-8"
    try:
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer,
                encoding=pref_enc,
                errors="replace",
                line_buffering=True,
            )
    except Exception:
        pass
    try:
        if hasattr(sys.stderr, "buffer"):
            sys.stderr = io.TextIOWrapper(
                sys.stderr.buffer,
                encoding=pref_enc,
                errors="replace",
                line_buffering=True,
            )
    except Exception:
        pass


fix_stdout_encoding()

import yaml


def parse_args():
    parser = argparse.ArgumentParser(
        description="CI Pipeline Health Inspector - 巡检CI流水线健康状态"
    )
    parser.add_argument(
        "repo_path",
        type=str,
        help="本地Git仓库路径",
    )
    parser.add_argument(
        "config_path",
        type=str,
        help="YAML配置文件路径",
    )
    parser.add_argument(
        "--results-dir",
        type=str,
        default=None,
        help="JSON格式的CI结果文件夹路径（默认从仓库的ci-logs目录读取）",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="只查看指定日期之后的运行记录，格式: YYYY-MM-DD",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="报告输出路径（默认输出到标准输出）",
    )
    parser.add_argument(
        "--compare",
        type=str,
        default=None,
        help="上一份Markdown巡检报告路径，用于对比异常变化",
    )
    return parser.parse_args()


def load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    if not config or "stages" not in config:
        print(f"错误: 配置文件 {config_path} 缺少 'stages' 字段", file=sys.stderr)
        sys.exit(1)
    stage_map = {}
    for stage in config["stages"]:
        name = stage.get("name")
        if not name:
            print("错误: 配置文件中存在未命名的阶段", file=sys.stderr)
            sys.exit(1)
        stage_map[name] = {
            "max_avg_duration_minutes": stage.get("max_avg_duration_minutes", float("inf")),
            "max_failure_rate_percent": stage.get("max_failure_rate_percent", 100),
            "min_sample_count": stage.get("min_sample_count", 1),
        }
    return {"pipeline": config.get("pipeline", {}).get("name", "default"), "stages": stage_map}


def find_ci_results(repo_path: str, results_dir: str | None) -> list[dict]:
    if results_dir:
        search_dir = Path(results_dir)
    else:
        search_dir = Path(repo_path) / "ci-logs"

    if not search_dir.exists():
        print(f"错误: CI结果目录不存在: {search_dir}", file=sys.stderr)
        sys.exit(1)

    results = []
    for filepath in sorted(search_dir.glob("*.json")):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "run_id" in data and "stages" in data:
                results.append(data)
        except (json.JSONDecodeError, KeyError) as e:
            print(f"警告: 跳过无效文件 {filepath.name}: {e}", file=sys.stderr)

    results.sort(key=lambda r: r.get("timestamp", ""))
    return results


def filter_by_since(results: list[dict], since: str | None) -> list[dict]:
    if not since:
        return results
    try:
        since_dt = datetime.fromisoformat(since)
    except ValueError:
        print(f"错误: 无效的日期格式: {since}，应为 YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)
    filtered = []
    for r in results:
        ts = r.get("timestamp", "")
        try:
            run_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)
            if run_dt >= since_dt:
                filtered.append(r)
        except (ValueError, AttributeError):
            continue
    return filtered


def compute_stats(results: list[dict], config: dict) -> dict:
    stage_names = list(config["stages"].keys())
    stats = {}
    for name in stage_names:
        durations = []
        failures_in_last_10 = 0
        total_in_last_10 = 0
        executed_in_last_10 = 0
        executions_in_last_5 = 0
        total_in_last_5 = 0
        all_durations = []
        failure_count = 0
        total_count = 0

        for i, run in enumerate(results):
            stage_results = {s["name"]: s for s in run.get("stages", [])}
            stage_info = stage_results.get(name)
            total_count += 1
            all_durations.append(stage_info)

            is_in_last_10 = len(results) - i <= 10
            is_in_last_5 = len(results) - i <= 5

            if stage_info is None:
                if is_in_last_10:
                    total_in_last_10 += 1
                if is_in_last_5:
                    total_in_last_5 += 1
                continue

            status = stage_info.get("status", "unknown")
            duration = stage_info.get("duration_minutes", 0)

            if status == "success":
                durations.append(duration)
            elif status == "failed":
                failure_count += 1
                durations.append(duration)

            total_in_last_10 += 1 if is_in_last_10 else 0
            total_in_last_5 += 1 if is_in_last_5 else 0
            if is_in_last_10 and status == "failed":
                failures_in_last_10 += 1
            if is_in_last_10 and status not in ("skipped", None):
                executed_in_last_10 += 1
            if is_in_last_5 and status not in ("skipped", None):
                executions_in_last_5 += 1

        avg_duration = sum(durations) / len(durations) if durations else 0
        failure_rate_last_10 = (failures_in_last_10 / executed_in_last_10 * 100) if executed_in_last_10 > 0 else 0

        stats[name] = {
            "avg_duration_minutes": round(avg_duration, 2),
            "failure_count": failure_count,
            "total_count": total_count,
            "failure_rate_last_10": round(failure_rate_last_10, 2),
            "executed_in_last_10": executed_in_last_10,
            "executed_in_last_5": executions_in_last_5,
            "total_in_last_5": total_in_last_5,
            "total_in_last_10": total_in_last_10,
            "failures_in_last_10": failures_in_last_10,
            "durations": durations,
        }
    return stats


def detect_dependency_issues(results: list[dict], config: dict) -> list[dict]:
    stage_names = list(config["stages"].keys())
    issues = []

    for run in results:
        stage_results = {s["name"]: s for s in run.get("stages", [])}
        for i in range(len(stage_names) - 1):
            current_name = stage_names[i]
            next_name = stage_names[i + 1]
            current = stage_results.get(current_name)
            nxt = stage_results.get(next_name)

            if current and nxt:
                if current.get("status") == "failed" and nxt.get("status") == "success":
                    issue = {
                        "run_id": run.get("run_id", "unknown"),
                        "failed_stage": current_name,
                        "succeeded_stage": next_name,
                        "timestamp": run.get("timestamp", "unknown"),
                    }
                    if issue not in issues:
                        issues.append(issue)
    return issues


def check_anomalies(stats: dict, config: dict) -> tuple[dict, dict]:
    anomalies = {}
    insufficient_data = {}
    for name, stage_stat in stats.items():
        stage_config = config["stages"].get(name, {})
        issues = []

        max_dur = stage_config.get("max_avg_duration_minutes", float("inf"))
        if stage_stat["avg_duration_minutes"] > max_dur:
            issues.append(
                f"平均耗时 {stage_stat['avg_duration_minutes']} 分钟超过上限 {max_dur} 分钟"
            )

        min_sample = stage_config.get("min_sample_count", 1)
        executed_count = stage_stat["executed_in_last_10"]
        if executed_count < min_sample:
            insufficient_data[name] = {
                "executed": executed_count,
                "required": min_sample,
            }
        else:
            max_fail = stage_config.get("max_failure_rate_percent", 100)
            if stage_stat["failure_rate_last_10"] > max_fail:
                issues.append(
                    f"最近10次失败率 {stage_stat['failure_rate_last_10']}% 超过上限 {max_fail}%"
                )

        if stage_stat["total_in_last_5"] > 0 and stage_stat["executed_in_last_5"] == 0:
            issues.append("最近5次运行中完全未执行")

        if issues:
            anomalies[name] = issues
    return anomalies, insufficient_data


def generate_bar(duration: float, max_bar_width: int = 30) -> str:
    if duration <= 0:
        return "(无数据)"
    bar_len = min(int(duration), max_bar_width)
    return "#" * bar_len + f" {duration:.1f}min"


def generate_report(stats: dict, anomalies: dict, insufficient_data: dict, dep_issues: list[dict], config: dict, results: list[dict], comparison: dict | None = None) -> str:
    lines = []
    pipeline_name = config.get("pipeline", "default")
    lines.append(f"# CI流水线健康巡检报告")
    lines.append("")
    lines.append(f"**流水线**: {pipeline_name}")
    lines.append(f"**巡检时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**分析运行数**: {len(results)}")
    lines.append("")

    lines.append("## 各阶段耗时柱状摘要")
    lines.append("")
    for name, stage_stat in stats.items():
        bar = generate_bar(stage_stat["avg_duration_minutes"])
        cfg = config["stages"].get(name, {})
        limit = cfg.get("max_avg_duration_minutes", "N/A")
        mark_parts = []
        if name in anomalies:
            mark_parts.append("[!]")
        if name in insufficient_data:
            mark_parts.append("[数据不足]")
        mark = " " + " ".join(mark_parts) if mark_parts else ""
        lines.append(f"### {name}{mark}")
        lines.append(f"```\n{bar}\n```")
        lines.append(f"- 平均耗时: {stage_stat['avg_duration_minutes']} 分钟 (上限: {limit} 分钟)")
        lines.append(f"- 累计失败次数: {stage_stat['failure_count']}/{stage_stat['total_count']}")
        lines.append(f"- 最近10次执行样本数: {stage_stat['executed_in_last_10']}/{stage_stat['total_in_last_10']} (失败: {stage_stat['failures_in_last_10']})")
        if name in insufficient_data:
            info = insufficient_data[name]
            lines.append(f"- 最近10次失败率: 数据不足 (实际执行 {info['executed']} 次，最小样本要求 {info['required']} 次)")
        else:
            lines.append(f"- 最近10次失败率: {stage_stat['failure_rate_last_10']}%")
        lines.append(f"- 最近5次执行次数: {stage_stat['executed_in_last_5']}/{stage_stat['total_in_last_5']}")
        lines.append("")

    lines.append("## 异常项清单")
    lines.append("")
    if anomalies:
        for name, issues in anomalies.items():
            lines.append(f"### [X] {name}")
            for issue in issues:
                lines.append(f"- {issue}")
            lines.append("")
    else:
        lines.append("[OK] 未发现异常")
        lines.append("")

    if dep_issues:
        lines.append("## 依赖关系配置异常")
        lines.append("")
        lines.append("> 以下记录中前序阶段失败但后续阶段仍然成功执行，说明阶段依赖关系配置可能有误。")
        lines.append("")
        lines.append("| 运行ID | 失败阶段 | 成功阶段 | 时间 |")
        lines.append("|--------|----------|----------|------|")
        for issue in dep_issues:
            lines.append(
                f"| {issue['run_id']} | {issue['failed_stage']} | {issue['succeeded_stage']} | {issue['timestamp']} |"
            )
        lines.append("")

    lines.append("## 建议关注排序")
    lines.append("")
    ranked = rank_anomalies(stats, anomalies, dep_issues, config)
    if ranked:
        for i, item in enumerate(ranked, 1):
            lines.append(f"{i}. **{item['stage']}** - {item['reason']}")
    else:
        lines.append("[OK] 流水线运行健康，无需特别关注")
    lines.append("")

    if comparison is not None:
        lines.append("## 变化摘要")
        lines.append("")
        persistent = comparison.get("persistent", [])
        new_anomalies = comparison.get("new", [])
        recovered = comparison.get("recovered", [])

        if persistent:
            lines.append("### 持续异常")
            lines.append("")
            for name in persistent:
                issues = anomalies.get(name, [])
                if issues:
                    lines.append(f"- **{name}**: {'; '.join(issues)}")
                else:
                    lines.append(f"- **{name}**")
            lines.append("")

        if new_anomalies:
            lines.append("### 新增异常")
            lines.append("")
            for name in new_anomalies:
                issues = anomalies.get(name, [])
                if issues:
                    lines.append(f"- **{name}**: {'; '.join(issues)}")
                else:
                    lines.append(f"- **{name}**")
            lines.append("")

        if recovered:
            lines.append("### 已恢复")
            lines.append("")
            for name in recovered:
                lines.append(f"- **{name}**")
            lines.append("")

        if not persistent and not new_anomalies and not recovered:
            lines.append("[OK] 与上次报告相比无变化")
            lines.append("")

    return "\n".join(lines)


def parse_previous_report(report_path: str) -> set[str]:
    if not report_path:
        return set()
    path = Path(report_path)
    if not path.exists():
        print(f"警告: 对比报告不存在: {report_path}", file=sys.stderr)
        return set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        print(f"警告: 读取对比报告失败: {e}", file=sys.stderr)
        return set()

    previous_anomalies = set()
    in_anomaly_section = False
    in_dep_section = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "## 异常项清单":
            in_anomaly_section = True
            in_dep_section = False
            continue
        if stripped == "## 依赖关系配置异常":
            in_anomaly_section = False
            in_dep_section = True
            continue
        if stripped.startswith("## ") and stripped not in ("## 异常项清单", "## 依赖关系配置异常"):
            in_anomaly_section = False
            in_dep_section = False
            continue
        if in_anomaly_section:
            if stripped.startswith("### [X] "):
                name = stripped[len("### [X] "):].strip()
                previous_anomalies.add(name)

    return previous_anomalies


def compare_anomalies(current_anomalies: dict, previous_anomalies: set[str]) -> dict:
    all_current = set(current_anomalies.keys())

    persistent = sorted(list(all_current & previous_anomalies))
    new_anomalies = sorted(list(all_current - previous_anomalies))
    recovered = sorted(list(previous_anomalies - all_current))

    return {
        "persistent": persistent,
        "new": new_anomalies,
        "recovered": recovered,
    }


def rank_anomalies(stats: dict, anomalies: dict, dep_issues: list[dict], config: dict) -> list[dict]:
    ranked = []

    for name, issues in anomalies.items():
        severity = 0
        reasons = []
        stage_stat = stats.get(name, {})
        stage_cfg = config["stages"].get(name, {})

        for issue in issues:
            if "未执行" in issue:
                severity += 30
                reasons.append("阶段最近5次未执行")
            elif "失败率" in issue:
                actual = stage_stat.get("failure_rate_last_10", 0)
                limit = stage_cfg.get("max_failure_rate_percent", 100)
                over = actual - limit
                severity += min(over, 30)
                reasons.append(f"失败率超标(+{over:.1f}%)")
            elif "耗时" in issue:
                actual = stage_stat.get("avg_duration_minutes", 0)
                limit = stage_cfg.get("max_avg_duration_minutes", float("inf"))
                over = actual - limit
                severity += min(over * 2, 20)
                reasons.append(f"耗时超标(+{over:.1f}min)")

        for dep in dep_issues:
            if dep["failed_stage"] == name:
                severity += 15
                reasons.append("存在依赖关系配置异常")
                break

        ranked.append({"stage": name, "severity": severity, "reason": "; ".join(reasons)})

    for dep in dep_issues:
        succeeded = dep["succeeded_stage"]
        if succeeded not in anomalies and not any(r["stage"] == succeeded for r in ranked):
            ranked.append(
                {"stage": succeeded, "severity": 10, "reason": "依赖关系异常中的后续成功阶段"}
            )

    ranked.sort(key=lambda x: x["severity"], reverse=True)
    return ranked


def main():
    args = parse_args()

    repo_path = Path(args.repo_path).resolve()
    if not repo_path.exists():
        print(f"错误: 仓库路径不存在: {repo_path}", file=sys.stderr)
        sys.exit(1)

    config = load_config(args.config_path)

    results = find_ci_results(str(repo_path), args.results_dir)
    if not results:
        print("错误: 未找到任何CI运行记录", file=sys.stderr)
        sys.exit(1)

    results = filter_by_since(results, args.since)
    if not results:
        print("错误: 过滤后无运行记录", file=sys.stderr)
        sys.exit(1)

    results = results[-30:]

    stats = compute_stats(results, config)
    dep_issues = detect_dependency_issues(results, config)
    anomalies, insufficient_data = check_anomalies(stats, config)

    comparison = None
    if args.compare:
        previous_anomalies = parse_previous_report(args.compare)
        comparison = compare_anomalies(anomalies, previous_anomalies)

    report = generate_report(stats, anomalies, insufficient_data, dep_issues, config, results, comparison)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"报告已生成: {output_path}")
    else:
        print(report)

    has_anomalies = len(anomalies) > 0
    has_dep_issues = len(dep_issues) > 0
    if has_anomalies or has_dep_issues:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
