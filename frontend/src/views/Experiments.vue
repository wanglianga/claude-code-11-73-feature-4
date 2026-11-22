<template>
  <div>
    <h2>规则实验 · 预算归因 · 发布门禁</h2>

    <!-- 实验目录 -->
    <div class="card">
      <h3>实验目录（阈值 / 计分公式 / 适用门店 / 候选生效期 / 审批状态）</h3>
      <div class="form-row">
        <button class="ghost" @click="loadExperimentSeed">灌入规则实验演示样本（10 会员/稀有门店 S003/补传交易）</button>
        <span v-if="seedMsg" class="muted">{{ seedMsg }}</span>
      </div>
      <table>
        <thead>
          <tr><th>id</th><th>编码</th><th>名称</th><th>状态</th><th>当前版本</th><th>批准版本</th><th>路线</th><th>更新时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in experiments" :key="e.id">
            <td class="mono">#{{ e.id }}</td>
            <td class="mono">{{ e.code }}</td>
            <td>{{ e.name }}</td>
            <td><span :class="['tag', statusClass(e.status)]">{{ e.status }}</span></td>
            <td class="mono">v{{ e.current_version_no }}</td>
            <td class="mono">{{ e.approved_version_no ? 'v' + e.approved_version_no : '—' }}</td>
            <td class="mono">{{ e.latest_sampling_mode }} · {{ e.latest_formula === 'SPEND_ACTIVE_COMPOSITE' ? '并重' : (e.latest_formula === 'SPEND_ONLY' ? '消费优先' : '门店门槛') }}</td>
            <td class="mono">{{ e.updated_at.slice(0, 16) }}</td>
            <td>
              <button class="ghost" @click="selectExperiment(e)">查看/运行</button>
              <button class="ghost" @click="submitTransition(e, 'SUBMITTED')" :disabled="e.status !== 'DRAFT'">提交</button>
              <button class="ghost" @click="submitTransition(e, 'APPROVED')" :disabled="e.status !== 'SUBMITTED'">批准</button>
              <button class="ghost" @click="submitTransition(e, 'REJECTED')" :disabled="e.status !== 'SUBMITTED'">驳回</button>
            </td>
          </tr>
        </tbody>
      </table>

      <h4 style="margin-top:14px">新建实验</h4>
      <div class="form-row">
        <input v-model="form.code" placeholder="编码 EXP-xxx" style="width:130px" />
        <input v-model="form.name" placeholder="名称" style="width:150px" />
        <select v-model="form.formula">
          <option value="SPEND_ACTIVE_COMPOSITE">消费额与活跃门店并重</option>
          <option value="SPEND_ONLY">消费额优先</option>
          <option value="ACTIVE_STORE_GATED">活跃门店双门槛</option>
        </select>
        <input v-model="form.thresholds" placeholder='阈值 JSON {"SILVER":400,"GOLD":1200}' style="width:230px" />
        <select v-model="form.sampling_mode">
          <option value="FULL">全量成员集</option>
          <option value="STRATIFIED">分层抽样</option>
        </select>
        <button @click="createExperiment">创建实验</button>
      </div>
      <p class="muted">
        并重公式默认权重 消费×1 + 活跃门店×100；分层抽样默认 40% 分层、95% 置信、门店相对误差≤25%、每店≥2 入样。
      </p>
    </div>

    <template v-if="current">
      <!-- 版本与参数 -->
      <div class="card">
        <h3>实验 #{{ current.id }} {{ current.name }} — 版本与参数指纹</h3>
        <table>
          <thead><tr><th>版本</th><th>公式</th><th>阈值</th><th>公式参数</th><th>门店</th><th>生效期</th><th>路线/抽样设计</th><th>参数指纹</th><th>备注</th></tr></thead>
          <tbody>
            <tr v-for="v in current.versions" :key="v.id">
              <td class="mono">v{{ v.version_no }}</td>
              <td class="mono">{{ v.formula }}</td>
              <td class="mono">{{ JSON.stringify(v.thresholds) }}</td>
              <td class="mono">{{ JSON.stringify(v.formula_params) }}</td>
              <td class="mono">{{ scopeText(v.store_scope) }}</td>
              <td class="mono">{{ v.candidate_effective_from.slice(0, 10) }}</td>
              <td class="mono">{{ v.sampling_mode }} {{ v.sampling_mode === 'STRATIFIED' ? JSON.stringify(v.sampling_design) : '' }}</td>
              <td class="mono" :title="v.param_fingerprint">{{ v.param_fingerprint.slice(0, 12) }}…</td>
              <td class="muted">{{ v.change_note }}</td>
            </tr>
          </tbody>
        </table>
        <div class="form-row" style="margin-top:10px">
          <input v-model="reviseThresholds" placeholder='改阈值即新版本，如 {"SILVER":500,"GOLD":1400}' style="width:280px" />
          <button @click="revise">修订阈值（旧结果保留但门禁判失效）</button>
        </div>
      </div>

      <!-- 影子运行 -->
      <div class="card">
        <h3>影子分群（同一快照并行现行 vs 候选规则）</h3>
        <div class="form-row">
          <label>业务截止</label><input v-model="runForm.biz_as_of" style="width:190px" />
          <label>系统水位</label><input v-model="runForm.sys_as_of" style="width:190px" />
          <select v-model="runForm.mode">
            <option value="AUTO">按版本路线</option><option value="FULL">全量 FULL</option><option value="SAMPLED">抽样 SAMPLED</option>
          </select>
          <button @click="runShadow">运行影子分群</button>
        </div>
        <table style="margin-top:8px">
          <thead><tr><th>运行</th><th>路线</th><th>快照</th><th>评估/全集</th><th>时间</th><th></th></tr></thead>
          <tbody>
            <tr v-for="r in runs" :key="r.id">
              <td class="mono">#{{ r.id }}</td>
              <td><span class="tag">{{ r.kind }}</span></td>
              <td class="mono">快照 #{{ r.snapshot_id }}</td>
              <td class="mono">{{ r.summary?.evaluated_members ?? '-' }} / {{ r.summary?.frame_population ?? '-' }}</td>
              <td class="mono">{{ r.created_at.slice(5, 16) }}</td>
              <td><button class="ghost" @click="openRun(r.id)">详情</button></td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- 运行详情 -->
    <template v-if="detail">
      <div class="card">
        <h3>影子运行 #{{ detail.id }}（{{ detail.kind }}）— 快照与样本</h3>
        <div class="grid cols-4">
          <div class="stat"><div class="num">{{ detail.snapshot.total_population }}</div><div class="label">冻结成员全集</div></div>
          <div class="stat"><div class="num">{{ detail.snapshot.sample_size }}</div><div class="label">入样成员</div></div>
          <div class="stat"><div class="num">{{ detail.snapshot.watermark_event_id }}</div><div class="label">证据水位 event#</div></div>
          <div class="stat"><div class="num">{{ detail.precision?.overall_decidable ? '可判定' : '含不可判定' }}</div><div class="label">精度结论</div></div>
        </div>
        <p class="muted" style="margin-top:8px">
          快照 v{{ detail.snapshot.version_no }}（前序 #{{ detail.snapshot.supersedes_snapshot_id ?? '无' }}），
          身份图 event#{{ detail.snapshot.identity_graph_version }}，规则集 event#{{ detail.snapshot.rule_set_version }}，
          抽样：{{ detail.snapshot.sampling_mode }} {{ JSON.stringify(detail.snapshot.sampling_design) }}
        </p>
        <p v-if="detail.snapshot.pending_members.length" class="muted" style="color:var(--red)">
          未决样本（身份拆分/合并审批中，不按合并消费计算）：
          <span v-for="p in detail.snapshot.pending_members" :key="p.member_key" class="tag bad" style="margin-right:6px">{{ p.member_key }}</span>
        </p>
      </div>

      <div class="card">
        <h3>等级迁移与逐人结果</h3>
        <div class="form-row">
          <span v-for="(v, k) in detail.summary.migration" :key="k" class="tag" style="margin-right:8px">
            {{ k }}: {{ v.estimate }}{{ detail.kind === 'SAMPLED' ? ` (${v.ci_low}~${v.ci_high})` : '' }}
          </span>
        </div>
        <table>
          <thead><tr><th>会员</th><th>门店</th><th>分层</th><th>现行等级</th><th></th><th>候选等级</th><th>候选分</th><th>迁移</th><th>成本差额(总)</th></tr></thead>
          <tbody>
            <tr v-for="m in detail.members" :key="m.member_key">
              <td class="mono">{{ m.member_key }}</td>
              <td class="mono">{{ m.home_store }}</td>
              <td class="mono">{{ m.stratum }}</td>
              <td><span :class="['tag', m.current_tier]">{{ m.current_tier }}</span></td>
              <td class="arrow">→</td>
              <td><span :class="['tag', m.candidate_tier]">{{ m.candidate_tier }}</span></td>
              <td class="mono">{{ m.candidate_score ?? '—' }}</td>
              <td><span :class="['tag', migClass(m.migration)]">{{ m.migration }}</span></td>
              <td class="mono">¥{{ m.cost_delta?.TOTAL ?? 0 }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="card">
        <h3>预算归因（升级降级 × 门店 × 权益类型 × 规则参数指纹）</h3>
        <table>
          <thead><tr><th>门店</th><th>迁移</th><th>权益</th><th>精确值</th><th>估计</th><th>95% 置信区间</th><th>人数</th><th>可判定</th><th>规则指纹</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="a in detail.attributions" :key="a.id">
              <td class="mono">{{ a.store }}</td>
              <td class="mono">{{ a.migration }}</td>
              <td class="mono">{{ a.benefit_type }}</td>
              <td class="mono">{{ a.exact_value !== null ? '¥' + a.exact_value : '—' }}</td>
              <td class="mono">{{ a.estimate !== null ? '¥' + a.estimate : '—' }}</td>
              <td class="mono">{{ a.ci_low !== null ? `¥${a.ci_low} ~ ¥${a.ci_high}` : '—' }}</td>
              <td class="mono">{{ a.member_count }}</td>
              <td><span :class="['tag', a.decidable ? 'ok' : 'bad']">{{ a.decidable ? '是' : '不可判定' }}</span></td>
              <td class="mono" :title="a.rule_fingerprint">{{ a.rule_fingerprint.slice(0, 10) }}…</td>
              <td class="muted">{{ a.note }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="card">
        <h3>精度评估（置信范围 / 未覆盖 / 全量基准对比）</h3>
        <p class="muted">
          覆盖 {{ detail.precision.covered_members }} 人，未覆盖 {{ detail.precision.uncovered_members }} 人，
          置信水平 {{ detail.precision.confidence_level }}
        </p>
        <h4>门店可判定性</h4>
        <table>
          <thead><tr><th>门店</th><th>框架/入样</th><th>相对误差</th><th>边界成员</th><th>可判定</th><th>原因</th></tr></thead>
          <tbody>
            <tr v-for="s in detail.precision.undecidable_stores" :key="s.store">
              <td class="mono">{{ s.store }}</td>
              <td class="mono">{{ s.frame_members }} / {{ s.sampled_members }}</td>
              <td class="mono">{{ s.rse ?? 0 }}</td>
              <td class="mono">{{ s.boundary_members ?? 0 }} / 入样 {{ s.boundary_sampled ?? 0 }}</td>
              <td><span :class="['tag', s.decidable ? 'ok' : 'bad']">{{ s.decidable ? '是' : '否' }}</span></td>
              <td class="muted">{{ (s.reasons || []).join('；') || '达标' }}</td>
            </tr>
          </tbody>
        </table>
        <div v-if="detail.precision.metrics.benchmark_vs_full" style="margin-top:10px">
          <h4>同快照全量基准对比（全量精确值 vs 抽样估计）</h4>
          <table>
            <thead><tr><th>指标</th><th>精确值</th><th>抽样估计</th><th>绝对误差</th><th>相对误差</th><th>真值在CI内</th></tr></thead>
            <tbody>
              <tr v-for="(m, k) in detail.precision.metrics.benchmark_vs_full.cost_delta" :key="'c'+k">
                <td class="mono">成本·{{ k }}</td><td class="mono">{{ m.exact }}</td><td class="mono">{{ m.estimate }}</td>
                <td class="mono">{{ m.abs_error }}</td><td class="mono">{{ m.rel_error }}</td>
                <td><span :class="['tag', m.inside_ci ? 'ok' : 'bad']">{{ m.inside_ci ? '是' : '否' }}</span></td>
              </tr>
              <tr v-for="(m, k) in detail.precision.metrics.benchmark_vs_full.tier_population" :key="'t'+k">
                <td class="mono">等级人数·{{ k }}</td><td class="mono">{{ m.exact }}</td><td class="mono">{{ m.estimate }}</td>
                <td class="mono">{{ m.abs_error }}</td><td class="mono">{{ m.rel_error }}</td>
                <td><span :class="['tag', m.inside_ci ? 'ok' : 'bad']">{{ m.inside_ci ? '是' : '否' }}</span></td>
              </tr>
            </tbody>
          </table>
        </div>
        <button class="ghost" style="margin-top:8px" @click="attachBenchmark" :disabled="detail.kind !== 'SAMPLED'">挂接同快照全量基准对比</button>
      </div>

      <div class="card">
        <h3>发布门禁与正式发布</h3>
        <div class="form-row">
          <button @click="runGate">执行门禁检查</button>
          <input v-model="releaseName" placeholder="正式发布名称，如 2026Q3 等级发布" style="width:240px" />
          <button @click="doRelease">门禁通过后正式发布</button>
        </div>
        <table v-if="gate" style="margin-top:8px">
          <thead><tr><th>检查项</th><th>结果</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="c in gate.checks" :key="c.code">
              <td class="mono">{{ c.code }}</td>
              <td><span :class="['tag', c.passed ? 'ok' : 'bad']">{{ c.passed ? '通过' : '拦截' }}</span></td>
              <td class="muted">{{ c.detail }}</td>
            </tr>
          </tbody>
        </table>

        <h4 style="margin-top:12px">预算数字全链路回查</h4>
        <div class="form-row">
          <input v-model="traceForm.store" placeholder="门店 S001" style="width:110px" />
          <select v-model="traceForm.benefit_type">
            <option>POINTS</option><option>BIRTHDAY</option><option>STORE_SUBSIDY</option><option>BASE</option>
          </select>
          <select v-model="traceForm.migration">
            <option value="">全部迁移</option><option>UPGRADE</option><option>DOWNGRADE</option><option>RETAIN</option>
          </select>
          <button class="ghost" @click="doTrace">回查实验配置/样本/规则版本/精度</button>
        </div>
        <pre v-if="trace">{{ traceSummary }}</pre>
      </div>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref, computed } from 'vue'
import { api } from '../api'

const experiments = ref([])
const current = ref(null)
const runs = ref([])
const detail = ref(null)
const gate = ref(null)
const trace = ref(null)
const releaseName = ref('2026Q3 候选规则正式发布')
const form = ref({
  code: '', name: '', formula: 'SPEND_ACTIVE_COMPOSITE',
  thresholds: '{"SILVER":400,"GOLD":1200}', sampling_mode: 'FULL',
})
const reviseThresholds = ref('')
const runForm = ref({ biz_as_of: '2026-04-30T23:59:00Z', sys_as_of: '2026-04-30T23:59:00Z', mode: 'AUTO' })
const traceForm = ref({ store: 'S001', benefit_type: 'POINTS', migration: '' })
const seedMsg = ref('')

async function loadExperimentSeed() {
  const r = await api.post('/seed/experiments')
  seedMsg.value = `已灌入 ${r.events_created} 个事件（跳过 ${r.events_skipped}），账本共 ${r.total_events} 事件`
  await load()
}

const traceSummary = computed(() => trace.value ? JSON.stringify({
  experiment: trace.value.experiment,
  rule_version: { version: trace.value.rule_version.version_no, fingerprint: trace.value.rule_version.param_fingerprint },
  sample_snapshot: { watermark: trace.value.sample_snapshot.watermark_event_id, population: trace.value.sample_snapshot.total_population, mode: trace.value.sample_snapshot.sampling_mode },
  precision_decidable: trace.value.precision?.overall_decidable,
  budget_rows: trace.value.budget_rows,
  lineage_member_count: trace.value.lineage_members.length,
}, null, 2) : '')

function statusClass(s) { return s === 'APPROVED' ? 'ok' : s === 'REJECTED' ? 'bad' : '' }
function migClass(m) { return m === 'UPGRADE' ? 'PLATINUM' : m === 'DOWNGRADE' ? 'bad' : m === 'PENDING' ? 'bad' : '' }
function scopeText(s) { return s === 'ALL' ? '全部门店' : (Array.isArray(s) ? s.join(',') : s) }

async function load() { experiments.value = await api.get('/experiments') }

async function createExperiment() {
  let thresholds
  try { thresholds = JSON.parse(form.value.thresholds) } catch { alert('阈值 JSON 格式错误'); return }
  const sampling_design = form.value.sampling_mode === 'STRATIFIED'
    ? { seed: 7, sample_fraction: 0.4, min_per_stratum: 1, max_relative_error: 0.25, min_store_sample: 2 } : {}
  const formula_params = form.value.formula === 'SPEND_ACTIVE_COMPOSITE'
    ? { weight_spend: 1.0, weight_active_store: 100.0 } : {}
  await api.post('/experiments', {
    code: form.value.code, name: form.value.name || form.value.code, formula: form.value.formula,
    thresholds, formula_params, window_days: 90,
    candidate_effective_from: '2026-07-01T00:00:00Z',
    sampling_mode: form.value.sampling_mode, sampling_design,
  })
  form.value.code = ''
  await load()
}

async function selectExperiment(e) {
  current.value = await api.get(`/experiments/${e.id}`)
  runs.value = await api.get(`/shadow/runs?experiment_id=${e.id}`)
  detail.value = null; gate.value = null; trace.value = null
}

async function submitTransition(e, target) {
  await api.post(`/experiments/${e.id}/transition`, { target })
  await load()
  if (current.value?.id === e.id) current.value = await api.get(`/experiments/${e.id}`)
}

async function revise() {
  let thresholds
  try { thresholds = JSON.parse(reviseThresholds.value) } catch { alert('阈值 JSON 格式错误'); return }
  await api.post(`/experiments/${current.value.id}/versions`, { thresholds, change_note: '前端修订阈值' })
  reviseThresholds.value = ''
  current.value = await api.get(`/experiments/${current.value.id}`)
  await load()
}

async function runShadow() {
  const r = await api.post(`/experiments/${current.value.id}/shadow`, { ...runForm.value })
  runs.value = await api.get(`/shadow/runs?experiment_id=${current.value.id}`)
  await openRun(r.id)
}

async function openRun(id) {
  detail.value = await api.get(`/shadow/runs/${id}`)
  gate.value = null; trace.value = null
  releaseName.value = `实验#${detail.value.experiment_version.experiment_id} 正式等级发布`
}

async function attachBenchmark() {
  await api.post(`/shadow/runs/${detail.value.id}/benchmark`)
  detail.value = await api.get(`/shadow/runs/${detail.value.id}`)
}

async function runGate() {
  gate.value = await api.post(`/shadow/runs/${detail.value.id}/gate`)
}

async function doRelease() {
  try {
    const r = await api.post(`/shadow/runs/${detail.value.id}/release`, { publication_name: releaseName.value })
    alert(`门禁通过，已生成正式发布 #${r.publication_id}`)
    await runGate()
  } catch (e) {
    alert('发布被门禁拦截：' + e.message)
    await runGate()
  }
}

async function doTrace() {
  const q = new URLSearchParams({ run_id: detail.value.id, store: traceForm.value.store, benefit_type: traceForm.value.benefit_type })
  if (traceForm.value.migration) q.set('migration', traceForm.value.migration)
  trace.value = await api.get(`/budget/trace?${q.toString()}`)
}

onMounted(load)
</script>
