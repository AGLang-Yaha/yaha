
## Waiter
- **Inherited:** None
- **Code:** Wtr
- **Model:** m
- **Preamble:**
    > hello
- **Work:**
    - **Wait Event:** go
    > work
- **Aftermath:**
    > done

## WaiterChild
- **Inherited:** Waiter
- **Code:** Wch
- **Work:**
    - **Inherited:** All

## WaiterOverride
- **Inherited:** Waiter
- **Code:** Wov
- **Work:**
    - **Inherited:** All
    - **Wait Event:** other

## Plain
- **Inherited:** None
- **Code:** Pln
- **Model:** m
- **Preamble:**
    > hello
- **Work:**
    > work
- **Aftermath:**
    > done
## Signaller
- **Inherited:** None
- **Code:** Sgl
- **Model:** m
- **Preamble:**
    > go
- **Work:**
    - **Exit Tools:**
        - set_event_success
    > work
- **Aftermath:**
    > done

## Looper
- **Inherited:** None
- **Code:** Lpr
- **Model:** m
- **Preamble:**
    > go
- **Work:**
    - **Exit Tools:**
        - set_event_loop
    > work
- **Aftermath:**
    > done

## Rearmer
- **Inherited:** None
- **Code:** Rrm
- **Model:** m
- **Preamble:**
    > hello
- **Work:**
    - **Wait Event:** go
    > work
- **Work2:**
    - **Wait Event:** go
    > work2
- **Aftermath:**
    > done

## LoopWaiter
- **Inherited:** None
- **Code:** Lwt
- **Model:** m
- **Preamble:**
    > hello
- **Work:**
    - **Wait Event:** go
    > work
- **Aftermath:**
    > done
### Toolset
	- loop_tool

