import { mount } from "svelte";
import ViewerChat from "./viewer/ViewerChat.svelte";

const target = document.getElementById("chat-app");

if (target) {
  mount(ViewerChat, { target });
}
