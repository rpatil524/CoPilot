package main

import (
	"chat-history/db"
	"chat-history/middleware"
	"chat-history/routes"
	"fmt"
	"net/http"

	chiMid "github.com/go-chi/chi/v5/middleware"
)

func main() {
	//init
	db.InitDB()

	// make router
	router := http.NewServeMux()

	// Health check endpoint
	router.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"status":"OK"}`))
	})

	router.HandleFunc("GET /user/{userId}", routes.GetUserConversations)
	router.HandleFunc("GET /conversation/{conversationId}", routes.GetConversation)
	router.HandleFunc("POST /conversation", routes.UpdateConversation)

	// create server with middleware
	port := ":8000"

	handler := middleware.ChainMiddleware(router,
		middleware.Logger(),
		middleware.Auth,
		chiMid.Recoverer,
	)
	s := http.Server{Addr: port, Handler: handler}

	fmt.Printf("Server running on port %s\n", port)
	s.ListenAndServe()
}
