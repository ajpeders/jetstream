import { mount } from "svelte";
import AdminRecent from "./admin/AdminRecent.svelte";

const target = document.getElementById("admin-recent-app");

if (target) {
  mount(AdminRecent, { target });
}
