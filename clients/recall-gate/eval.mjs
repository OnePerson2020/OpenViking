#!/usr/bin/env node
// Re-run when bjev changes or before moving a threshold: scores the labelled prompts with the
// gate's own questions and prints separation (AUC), the lowest need score of a prompt that needs
// memory (SKIP_BELOW must stay under it) and the tier mix. Needs LocalBridge on 127.0.0.1:8321.
// Labels: [prompt, depends on earlier turns (1/0), needs memory (1/0, null = not labelled)].
import { decide, score } from "./hook.mjs";

const CASES = [
  ["继续", 1, 0],
  ["好的", 1, 0],
  ["把这个函数改成 async", 1, 0],
  ["python 怎么反转列表", 0, 0],
  ["提交一下", 1, 0],
  ["what is a monad", 0, 0],
  ["V2 常数分那个问题后来怎么样了", 0, 1],
  ["帮我看下 Norbert 打分 job 现在什么状态", 0, 1],
  ["LocalBridge 上次部署到 devbox 是哪个 commit", 0, 1],
  ["上周周报放哪个文件夹", 0, 1],
  ["US-TTP 上 Dorado 写表又失败了", 0, 1],
  ["那个表现在有数据了吗", 1, 1],
  ["按我之前说的方式改", 1, 1],
  ["你觉得呢", 1, 0],
  ["再跑一遍", 1, 0],
  ["帮我写个周报", 0, 1],
  ["这个报错什么意思 TypeError: cannot read property of undefined", 0, 0],
  ["J7 的复购特征加到 200 个了吗", 0, 1],
  ["用 laya 还是 bjev 好", 0, 1],
  ["帮我查一下 bnpm 上 @mayx23/localbridge 最新版本", 0, 1],
  ["解释一下 listwise 训练", 0, 0],
  ["ultrathink 重新设计这个 hook", 1, 0],
  ["我导师是谁", 0, 1],
  ["给 Automatica 那篇论文改一下引言", 0, 1],
  ["rm -rf node_modules 然后重装", 0, 0],
  ["现在进度如何？", 1, null],
  ["你派的inj2使用的是什么模型?", 1, null],
  ["目前各个agent的状态及其任务如何?", 1, null],
  ["有哪些agent的的输出被截断而不是正常结束呢？", 1, null],
  ["我好担心完不成任务, 完不成心中的期待", 0, null],
  ["ccr,magpie,sub2api 对于 chatgpt订阅是如何处理的呢?", 0, null],
  ["我希望openViking和hindsight结合，有什么建议吗？", 0, null],
  ["请继续, 帮我优化; 4-6 希望优先保证可用性", 1, null],
  ["我有一个需求是, 主agent使用订阅,但是明确指定使用localbridge中的模型作为subagent去完成任务", 0, null],
  ["是否可以通过设置codex 的参数,来实现子agent任务不加密呢?", 0, null],
  ["那目前看,是不是v2是不是不太合适了,改为v1更合适一些?", 1, null],
  ["让这些用例在分享版构建下跳过", 1, null],
  ["开启思考和不开启思考的用时、Token 消耗大概是什么样子？然后思考强度开的是哪一档呢？", 1, null],
  ["删掉过期了吧", 1, null],
  ["帮我检查一下是不是后台有程序在偷偷跑 Gemini 2.5 Flash 呢？", 0, null],
  ["你是如何访问的modelhub? 用的哪个key?", 1, null],
  ["那这部分是不是可以直接不调用 LM，直接用规则来去处理？或者说这部分直接从样本中删除是不是更好一些？", 1, null],
  ["检查 runner 回溯 3039992 槽位 2026101600（DROP_FAKE build 验证）是否完成，完成则 fetch 结果并汇报", 0, null],
  ["等一下,不要再跑新的dorado了", 1, null],
  ["合规，请继续;", 1, null],
  ["帮我做一下吧", 1, null],
  ["提;推;另外,上游似乎又接受了一批pr", 1, null],
  ["是在多大的样本上做的测试呢? 分数又是如何打的呢?", 1, null],
  ["公开备份是什么? fork分支吗?", 1, null],
  ["请找一下tiktok租户下的", 1, null],
  ["该租户下,所以schema都不行吗?", 1, null],
  ["这次对比与上次有何不同? 为什么这次更公平?", 1, null],
  ["我记得有做过一个 豆包 LUNA 和 Gemini 2.5 Flash 三者的公平对比，那个的结果如何呢？", 0, null],
  ["请继续,帮我优化;优化完再review一下", 1, null],
  ["目前进度如何,请继续完成", 1, null],
  ["新加坡的节奏虽然慢,但是我给自己的压力很大", 0, null],
  ["我仍然希望使用 Archify 再尝试下", 1, null],
  ["html优化好了嘛; 请使用html的方式与我沟通", 1, null],
];

const auc = (pos, neg) => pos.reduce((s, p) => s + neg.reduce((t, n) => t + (p > n) + (p === n) / 2, 0), 0) / (pos.length * neg.length);
const rows = [];
// the gate's 1 s timeout is right for a hook but not for an eval: retry slow calls
const scoreRetry = async (prompt, tries = 3) => { try { return await score(prompt); } catch (e) { if (tries <= 1) throw e; return scoreRetry(prompt, tries - 1); } };
for (const [prompt, dependsOnContext, needsMemory] of CASES) rows.push({ prompt, dependsOnContext, needsMemory, ...(await scoreRetry(prompt)) });
const by = (key, label) => rows.filter((r) => r[label] != null).map((r) => [r[label], r[key]]);
const sep = (pairs) => auc(pairs.filter(([l]) => l).map(([, s]) => s), pairs.filter(([l]) => !l).map(([, s]) => s)).toFixed(2);
const tiers = {};
for (const r of rows) tiers[decide(r.need, r.ctx)] = (tiers[decide(r.need, r.ctx)] || 0) + 1;
console.log(`need AUC ${sep(by("need", "needsMemory"))}, lowest need score of a needs-memory prompt ${Math.min(...by("need", "needsMemory").filter(([l]) => l).map(([, s]) => s)).toFixed(2)}`);
console.log(`ctx  AUC ${sep(by("ctx", "dependsOnContext"))}`);
console.log("tiers", tiers);
