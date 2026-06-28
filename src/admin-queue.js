import { mount } from "svelte";
import AdminQueue from "./admin/AdminQueue.svelte";

const target = document.getElementById("admin-queue-app");

if (target) {
  mount(AdminQueue, { target });
}
