import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import Login from './views/Login.vue'
import Dashboard from './views/Dashboard.vue'
import Events from './views/Events.vue'
import Eligibility from './views/Eligibility.vue'
import Identity from './views/Identity.vue'
import Rules from './views/Rules.vue'
import Publications from './views/Publications.vue'
import Experiments from './views/Experiments.vue'
import './style.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: Login },
    { path: '/', component: Dashboard },
    { path: '/events', component: Events },
    { path: '/eligibility', component: Eligibility },
    { path: '/identity', component: Identity },
    { path: '/rules', component: Rules },
    { path: '/publications', component: Publications },
    { path: '/experiments', component: Experiments },
  ],
})

router.beforeEach((to) => {
  if (to.path !== '/login' && !localStorage.getItem('token')) return '/login'
})

createApp(App).use(router).mount('#app')
