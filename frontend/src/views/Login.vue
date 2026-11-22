<template>
  <div class="login-wrap">
    <div class="card">
      <h2>双时间会员资格历史服务</h2>
      <p class="muted" style="text-align:center">连锁药房 · 会员分析团队</p>
      <input v-model="username" placeholder="账号（admin / analyst）" @keyup.enter="doLogin" />
      <input v-model="password" type="password" placeholder="密码" @keyup.enter="doLogin" />
      <div v-if="err" class="err">{{ err }}</div>
      <button @click="doLogin">登录</button>
      <p class="muted" style="margin-top:12px">演示账号：admin / admin123，analyst / analyst123</p>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'

const router = useRouter()
const username = ref('admin')
const password = ref('')
const err = ref('')

async function doLogin() {
  err.value = ''
  try {
    const r = await api.login(username.value, password.value)
    localStorage.setItem('token', r.token)
    localStorage.setItem('username', r.username)
    localStorage.setItem('role', r.role)
    location.href = '/'
  } catch (e) {
    err.value = e.message
  }
}
</script>
