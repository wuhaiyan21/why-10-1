#!/usr/bin/env python3
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

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
            if is_in_last_5 and status not in ("skipped", None):
                executions_in_last_5 += 1

        avg_duration = sum(durations) / len(durations) if durations else 0
        failure_rate_last_10 = (failures_in_last_10 / total_in_last_10 * 100) if total_in_last_10 > 0 else 0

        stats[name] = {
            "avg_duration_minutes": round(avg_duration, 2),
            "failure_count": failure_count,
            "total_count": total_count,
            "failure_rate_last_10": round(failure_rate_last_10, 2),
            "executed_in_last_5": executions_in_last_5,
            "total_in_last_5": total_in_last_5,
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


def check_anomalies(stats: dict, config: dict) -> dict:
    anomalies = {}
    for name, stage_stat in stats.items():
        stage_config = config["stages"].get(name, {})
        issues = []

        max_dur = stage_config.get("max_avg_duration_minutes", float("inf"))
        if stage_stat["avg_duration_minutes"] > max_dur:
            issues.append(
                f"平均耗时 {stage_stat['avg_duration_minutes']} 分钟超过上限 {max_dur} 分钟"
            )

        max_fail = stage_config.get("max_failure_rate_percent", 100)
        if stage_stat["failure_rate_last_10"] > max_fail:
            issues.append(
                f"最近10次失败率 {stage_stat['failure_rate_last_10']}% 超过上限 {max_fail}%"
            )

        if stage_stat["total_in_last_5"] > 0 and stage_stat["executed_in_last_5"] == 0:
            issues.append("最近5次运行中完全未执行")

        if issues:
            anomalies[name] = issues
    return anomalies


def generate_bar(duration: float, max_bar_width: int = 30) -> str:
    if duration <= 0:
        return "(无数据)"
    bar_len = min(int(duration), max_bar_width)
    return "#" * bar_len + f" {duration:.1f}min"


def generate_report(stats: dict, anomalies: dict, dep_issues: list[dict], config: dict, results: list[dict]) -> str:
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
        mark = " [!]" if name in anomalies else ""
        lines.append(f"### {name}{mark}")
        lines.append(f"```\n{bar}\n```")
        lines.append(f"- 平均耗时: {stage_stat['avg_duration_minutes']} 分钟 (上限: {limit} 分钟)")
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

    return "\n".join(lines)


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
    anomalies = check_anomalies(stats, config)
    report = generate_report(stats, anomalies, dep_issues, config, results)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"报告已生成: {output_path}")
    else:
        sys.stdout.buffer.write(report.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")


if __name__ == "__main__":
    main()
