<template>
  <div>
    <h2>规则中心（版本化 · 修订链 · 部分撤回）</h2>

    <div class="card">
      <h3>规则版本</h3>
      <table>
        <thead>
          <tr><th>规则</th><th>版本</th><th>修订链</th><th>门店范围</th><th>商品范围</th><th>窗口</th><th>阈值</th><th>生效区间</th><th>入账时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="v in versions" :key="v.id">
            <td>{{ v.rule_id }}</td>
            <td class="mono">v{{ v.version }}</td>
            <td class="mono">{{ v.supersedes_version ? '↳ v' + v.supersedes_version : '初始' }}</td>
            <td>{{ scope(v.store_scope) }}</td>
            <td>{{ scope(v.product_scope) }}</td>
            <td>{{ v.window_days }} 天</td>
            <td class="mono">{{ JSON.stringify(v.thresholds) }}</td>
            <td class="mono">{{ v.effective_from.slice(0, 10) }} → {{ v.effective_to ? v.effective_to.slice(0, 10) : '开放' }}</td>
            <td class="mono">{{ v.sys_from.slice(0, 10) }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted">
        解析规则：对门店 s 与业务时点 T，取「生效区间覆盖 T、未被撤回、门店在范围内」的版本中
        (生效起点, 版本号) 最大者 —— 并发修订（如 v2/v3 同链修订 v1）由该全序确定性解决。
      </p>
    </div>

    <div class="card">
      <h3>撤回记录（部分追溯撤回）</h3>
      <table>
        <thead>
          <tr><th>规则</th><th>目标版本</th><th>门店范围</th><th>撤回业务区间</th><th>入账时间</th><th>事由</th></tr>
        </thead>
        <tbody>
          <tr v-for="w in withdrawals" :key="w.id">
            <td>{{ w.rule_id }}</td>
            <td class="mono">v{{ w.target_version }}</td>
            <td>{{ scope(w.store_scope) }}</td>
            <td class="mono">{{ w.withdraw_from.slice(0, 10) }} → {{ w.withdraw_to ? w.withdraw_to.slice(0, 10) : '开放' }}</td>
            <td class="mono">{{ w.sys_from.slice(0, 10) }}</td>
            <td class="muted">{{ w.reason }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const versions = ref([])
const withdrawals = ref([])

function scope(s) { return s === 'ALL' ? '全部门店/品类' : (Array.isArray(s) ? s.join(', ') : s) }

onMounted(async () => {
  versions.value = await api.get('/rules/versions')
  withdrawals.value = await api.get('/rules/withdrawals')
})
</script>
